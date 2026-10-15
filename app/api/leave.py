"""Leave: balances, applications, approvals."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, ready_staff
from app.leave import MAX_REQUEST_DAYS, balances_for, count_days, type_by_code, types_for
from app.models import LeaveRequest, Staff, XeroLeaveBalance

router = APIRouter(prefix="/api", tags=["leave"])


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def _request_out(r: LeaveRequest, staff: Staff | None = None, names: dict[str, str] | None = None) -> dict:
    lt = type_by_code(staff, r.leave_type) if staff else None
    return {
        "id": r.id,
        "staff_id": r.staff_id,
        "name": staff.name if staff else None,
        "leave_type": r.leave_type,
        "type_label": lt.label if lt else r.leave_type,
        "start_date": r.start_date.isoformat(),
        "end_date": r.end_date.isoformat(),
        "start_half": r.start_half,
        "end_half": r.end_half,
        "days": float(r.days),
        "reason": r.reason,
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "decided_by": (names or {}).get(r.decided_by, r.decided_by) if r.decided_by else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decision_note": r.decision_note,
    }


def _balances_out(db: Session, staff: Staff, history: int = 0):
    b = balances_for(db, staff, today())
    return [x.to_dict(history) for x in b] if b is not None else None


# App leave type -> words that identify the matching Xero leave type.
XERO_MATCH = {"annual": ("annual",), "personal": ("personal", "carer", "sick"), "long_service": ("long service",)}


def _xero_out(db: Session, staff: Staff) -> list[dict] | None:
    if staff.location_type != "onshore":
        return None
    rows = db.query(XeroLeaveBalance).filter_by(staff_id=staff.staff_id).order_by(XeroLeaveBalance.leave_name).all()
    if not rows:
        return None
    return [{"name": b.leave_name, "units": float(b.units), "unit_type": b.unit_type,
             "fetched_at": b.fetched_at.isoformat()} for b in rows]


def _xero_note(db: Session, staff: Staff, leave_type: str) -> str:
    rows = db.query(XeroLeaveBalance).filter_by(staff_id=staff.staff_id).all()
    words = XERO_MATCH.get(leave_type, ())
    match = next((b for b in rows if any(w in b.leave_name.lower() for w in words)), None)
    if match is None:
        return "Balance is in Xero."
    return f"Xero balance: {float(match.units):g} {match.unit_type.lower()} of {match.leave_name} (as of {match.fetched_at:%d %b})"


@router.get("/leave/me")
def my_leave(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    requests = (db.query(LeaveRequest).filter_by(staff_id=staff.staff_id)
                .order_by(LeaveRequest.start_date.desc(), LeaveRequest.id.desc()).limit(50).all())
    admins = {s.staff_id: s.name for s in db.query(Staff).filter_by(role="admin")}
    return {
        "location_type": staff.location_type,
        "join_date": staff.join_date.isoformat() if staff.join_date else None,
        "types": [{"code": t.code, "label": t.label, "has_balance": t.accrual is not None}
                  for t in types_for(staff)],
        "balances": _balances_out(db, staff, history=12),
        "xero_balances": _xero_out(db, staff),
        "requests": [_request_out(r, staff, admins) for r in requests],
        "today": today().isoformat(),
    }


class LeaveIn(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    start_half: bool = False
    end_half: bool = False
    reason: str = Field("", max_length=500)


@router.post("/leave/requests")
def apply_for_leave(body: LeaveIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    lt = type_by_code(staff, body.leave_type)
    if lt is None:
        raise HTTPException(400, "That leave type isn't available to you.")
    if body.end_date < body.start_date:
        raise HTTPException(400, "The end date is before the start date.")
    if (body.end_date - body.start_date).days >= MAX_REQUEST_DAYS:
        raise HTTPException(400, f"One request can cover at most {MAX_REQUEST_DAYS} days. Split it into two.")
    if body.start_date < today() - timedelta(days=60):
        raise HTTPException(400, "That's more than 60 days ago. Ask an admin to record it.")
    days = count_days(body.start_date, body.end_date, body.start_half, body.end_half)
    if days <= 0:
        raise HTTPException(400, "Those dates don't include any working days (Mon–Fri).")

    # Lock this person's row so two quick submits can't both pass the overlap check.
    db.query(Staff).filter_by(staff_id=staff.staff_id).with_for_update().one()
    clash = (db.query(LeaveRequest)
             .filter(LeaveRequest.staff_id == staff.staff_id,
                     LeaveRequest.status.in_(("pending", "approved")),
                     LeaveRequest.start_date <= body.end_date,
                     LeaveRequest.end_date >= body.start_date)
             .first())
    if clash:
        db.rollback()
        raise HTTPException(409, f"You already have leave requested for {clash.start_date:%d %b}–{clash.end_date:%d %b}. "
                                 "Cancel that one first if you need to change it.")
    req = LeaveRequest(staff_id=staff.staff_id, leave_type=lt.code, start_date=body.start_date,
                       end_date=body.end_date, start_half=body.start_half, end_half=body.end_half,
                       days=days, reason=body.reason.strip(), status="pending")
    db.add(req)
    db.commit()
    return _request_out(req, staff)


@router.post("/leave/requests/{request_id}/cancel")
def cancel_leave(request_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    req = db.query(LeaveRequest).filter_by(id=request_id, staff_id=staff.staff_id).with_for_update().first()
    if req is None:
        raise HTTPException(404, "Request not found.")
    if req.status != "pending":
        raise HTTPException(409, "Only requests still waiting for approval can be cancelled here. "
                                 "Ask an admin to change approved leave.")
    req.status = "cancelled"
    req.decided_at = datetime.now(UTC)
    db.commit()
    return _request_out(req, staff)


# ---- admin --------------------------------------------------------------------

def _with_balance_check(db: Session, r: LeaveRequest, staff: Staff, balances_cache: dict) -> dict:
    out = _request_out(r, staff)
    if staff.staff_id not in balances_cache:
        balances_cache[staff.staff_id] = balances_for(db, staff, today())
    bals = balances_cache[staff.staff_id]
    out["balance"] = None
    if bals is not None:
        b = next((x for x in bals if x.code == r.leave_type), None)
        if b is not None:
            after = b.available - Decimal(r.days) if r.status == "pending" else b.available
            out["balance"] = {"available": float(b.available), "after": float(after),
                              "short_by": float(-after) if after < 0 else 0}
    elif staff.location_type == "offshore":
        out["balance_note"] = "No join date set, so no balance yet."
    else:
        out["balance_note"] = _xero_note(db, staff, r.leave_type)
    return out


@router.get("/admin/leave")
def admin_leave(_: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    staff = {s.staff_id: s for s in db.query(Staff).all()}
    names = {sid: s.name for sid, s in staff.items()}
    cache: dict = {}
    pending = (db.query(LeaveRequest).filter_by(status="pending")
               .order_by(LeaveRequest.start_date, LeaveRequest.id).all())
    since = today() - timedelta(days=60)
    recent = (db.query(LeaveRequest)
              .filter(LeaveRequest.status.in_(("approved", "declined", "cancelled")), LeaveRequest.end_date >= since)
              .order_by(LeaveRequest.start_date.desc()).limit(100).all())
    offshore = sorted((s for s in staff.values() if s.location_type == "offshore" and s.active), key=lambda s: s.name)
    return {
        "today": today().isoformat(),
        "pending": [_with_balance_check(db, r, staff[r.staff_id], cache) for r in pending],
        "recent": [_request_out(r, staff[r.staff_id], names) for r in recent],
        "balances": [{"staff_id": s.staff_id, "name": s.name,
                      "join_date": s.join_date.isoformat() if s.join_date else None,
                      "balances": _balances_out(db, s, history=24)} for s in offshore],
    }


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|decline)$")
    note: str = Field("", max_length=500)


@router.post("/admin/leave/{request_id}/decide")
def decide(request_id: int, body: DecisionIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    req = db.query(LeaveRequest).filter_by(id=request_id).with_for_update().first()
    if req is None:
        raise HTTPException(404, "Request not found.")
    if req.status != "pending":
        raise HTTPException(409, f"This request was already {req.status}.")
    req.status = "approved" if body.decision == "approve" else "declined"
    req.decided_by = admin.staff_id
    req.decided_at = datetime.now(UTC)
    req.decision_note = body.note.strip()
    db.commit()
    return _request_out(req, db.get(Staff, req.staff_id))
