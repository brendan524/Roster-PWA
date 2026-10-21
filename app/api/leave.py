"""Leave: balances, applications, approvals."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import mailer
from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, ready_staff
from app.leave import (
    MAX_BACKDATE_DAYS,
    NOTICE_DAYS,
    MAX_REQUEST_DAYS,
    balances_for,
    count_days,
    is_long_service,
    label_for,
    long_service_ok,
    type_by_code,
    types_for,
)
from app.models import LeaveAttachment, LeaveRequest, Staff, XeroEmployeeLink, XeroLeaveBalance

router = APIRouter(prefix="/api", tags=["leave"])


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def _attachments(db: Session, request_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not request_ids:
        return out
    rows = (db.query(LeaveAttachment.id, LeaveAttachment.request_id, LeaveAttachment.filename,
                     LeaveAttachment.content_type, LeaveAttachment.size)
            .filter(LeaveAttachment.request_id.in_(request_ids)).order_by(LeaveAttachment.id))
    for a in rows:
        out.setdefault(a.request_id, []).append(
            {"id": a.id, "filename": a.filename, "content_type": a.content_type, "size": a.size})
    return out


def _request_out(r: LeaveRequest, staff: Staff | None = None, names: dict[str, str] | None = None,
                 files: dict[int, list[dict]] | None = None) -> dict:
    return {
        "attachments": (files or {}).get(r.id, []),
        "id": r.id,
        "short_notice": r.short_notice,
        "requested_by": (names or {}).get(r.requested_by, r.requested_by) if r.requested_by else None,
        "staff_id": r.staff_id,
        "name": staff.name if staff else None,
        "leave_type": r.leave_type,
        "type_label": label_for(staff, r.leave_type) if staff else r.leave_type,
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


def service_start(db: Session, staff: Staff) -> date | None:
    """Employment start for long service: Xero's start date if linked, else the app's join date."""
    link = db.get(XeroEmployeeLink, staff.staff_id)
    return (link.start_date if link and link.start_date else None) or staff.join_date


def _long_service_ok(db: Session, staff: Staff) -> bool:
    return staff.location_type == "onshore" and long_service_ok(service_start(db, staff), today())


def _is_casual(db: Session, staff: Staff) -> bool:
    """Casual in Xero payroll (onshore only)."""
    link = db.get(XeroEmployeeLink, staff.staff_id) if staff.location_type == "onshore" else None
    return bool(link and link.employment_basis == "CASUAL")


def _xero_out(db: Session, staff: Staff) -> list[dict] | None:
    if staff.location_type != "onshore" or not staff.has_leave:
        return None
    rows = db.query(XeroLeaveBalance).filter_by(staff_id=staff.staff_id).order_by(XeroLeaveBalance.leave_name).all()
    if not _long_service_ok(db, staff):  # kept hidden until 7 years' service
        rows = [b for b in rows if not is_long_service(b.leave_name)]
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


def _dates_text(r: LeaveRequest) -> str:
    def one(d: date, half: bool) -> str:
        return d.strftime("%a %-d %b %Y") + (" (half day)" if half else "")
    if r.start_date == r.end_date:
        return one(r.start_date, r.start_half or r.end_half)
    return f"{one(r.start_date, r.start_half)} to {one(r.end_date, r.end_half)}"


@router.get("/leave/me")
def my_leave(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    requests = (db.query(LeaveRequest).filter_by(staff_id=staff.staff_id)
                .order_by(LeaveRequest.start_date.desc(), LeaveRequest.id.desc()).limit(50).all())
    admins = {s.staff_id: s.name for s in db.query(Staff).filter_by(role="admin")}
    files = _attachments(db, [r.id for r in requests])
    return {
        "location_type": staff.location_type,
        "has_leave": staff.has_leave,
        "casual": _is_casual(db, staff),
        "join_date": staff.join_date.isoformat() if staff.join_date else None,
        "types": [{"code": t.code, "label": t.label, "has_balance": t.accrual is not None,
                   "advance_only": t.advance_only} for t in types_for(staff, _long_service_ok(db, staff), _is_casual(db, staff))],
        "max_backdate_days": MAX_BACKDATE_DAYS,
        "notice_days": NOTICE_DAYS,
        "balances": _balances_out(db, staff, history=12),
        "xero_balances": _xero_out(db, staff),
        "requests": [_request_out(r, staff, admins, files) for r in requests],
        "today": today().isoformat(),
    }


class LeaveIn(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    start_half: bool = False
    end_half: bool = False
    reason: str = Field("", max_length=500)
    short_notice: bool = False  # they've acknowledged it's inside the notice period and may not be approved


@router.post("/leave/requests")
def apply_for_leave(body: LeaveIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    if not staff.has_leave:
        raise HTTPException(400, "Contractors don't take leave through the app — talk to Brendan or Aaron.")
    lt = type_by_code(staff, body.leave_type, _long_service_ok(db, staff), _is_casual(db, staff))
    if lt is None:
        raise HTTPException(400, "That leave type isn't available to you.")
    if body.end_date < body.start_date:
        raise HTTPException(400, "The end date is before the start date.")
    if (body.end_date - body.start_date).days >= MAX_REQUEST_DAYS:
        raise HTTPException(400, f"One request can cover at most {MAX_REQUEST_DAYS} days. Split it into two.")
    if len(body.reason.strip()) < 3:
        raise HTTPException(400, "Please give a reason for your leave.")
    short = lt.advance_only and body.start_date < today() + timedelta(days=NOTICE_DAYS)
    if short and not body.short_notice:
        earliest = today() + timedelta(days=NOTICE_DAYS)
        raise HTTPException(400, f"{lt.label} needs at least {NOTICE_DAYS} days' notice — the earliest start date "
                                 f"is {earliest:%a %-d %b}. For anything sooner, tick the short-notice box "
                                 "(it may not be approved).")
    if short and body.start_date < today():
        raise HTTPException(400, f"{lt.label} can't start in the past.")
    if body.start_date < today() - timedelta(days=MAX_BACKDATE_DAYS):
        raise HTTPException(400, f"That's more than {MAX_BACKDATE_DAYS} days ago. Ask an admin to record it.")
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
                       days=days, reason=body.reason.strip(), status="pending", short_notice=bool(short))
    db.add(req)
    db.flush()
    mailer.queue_admins(
        db, f"{'SHORT NOTICE ' if short else ''}Leave request: {staff.name} — {lt.label.lower()}, {_dates_text(req)}",
        (f"Short notice: this starts in less than {NOTICE_DAYS} days. {staff.name} has acknowledged it may not be "
         "approved.\n\n" if short else "")
        + f"{staff.name} ({staff.staff_id}) has asked for {lt.label.lower()}:\n\n"
        f"  {_dates_text(req)} ({float(days):g} working day{'s' if days != 1 else ''})\n"
        f"  Reason: {req.reason}\n\nApprove or decline it on the Leave tab in the staff app.",
        exclude=staff.staff_id)
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

def _with_balance_check(db: Session, r: LeaveRequest, staff: Staff, balances_cache: dict,
                        files: dict[int, list[dict]] | None = None, names: dict[str, str] | None = None) -> dict:
    out = _request_out(r, staff, names, files)
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
def admin_leave(admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    staff = {s.staff_id: s for s in db.query(Staff).all()}
    names = {sid: s.name for sid, s in staff.items()}
    cache: dict = {}
    pending = (db.query(LeaveRequest).filter_by(status="pending")
               .order_by(LeaveRequest.start_date, LeaveRequest.id).all())
    since = today() - timedelta(days=60)
    recent = (db.query(LeaveRequest)
              .filter(LeaveRequest.status.in_(("approved", "declined", "cancelled")), LeaveRequest.end_date >= since)
              .order_by(LeaveRequest.start_date.desc()).limit(100).all())
    offshore = sorted((s for s in staff.values() if s.location_type == "offshore" and s.active and s.has_leave),
                      key=lambda s: s.name)
    files = _attachments(db, [r.id for r in pending + recent])
    return {
        "today": today().isoformat(),
        "pending": [_with_balance_check(db, r, staff[r.staff_id], cache, files, names) for r in pending],
        "recent": [_request_out(r, staff[r.staff_id], names, files) for r in recent],
        "balances": [{"staff_id": s.staff_id, "name": s.name,
                      "join_date": s.join_date.isoformat() if s.join_date else None,
                      "balances": _balances_out(db, s, history=24)} for s in offshore],
        "can_approve_on_behalf": admin.staff_id in on_behalf_approvers(),
        # For "Record leave for someone": who, and which leave types each can take.
        "people": [{"staff_id": s.staff_id, "name": s.name,
                    "types": [{"code": t.code, "label": t.label}
                              for t in types_for(s, _long_service_ok(db, s), _is_casual(db, s))]}
                   for s in sorted(staff.values(), key=lambda s: s.name.lower())
                   if s.active and s.has_leave and s.staff_id != admin.staff_id],
    }


def on_behalf_approvers() -> set[str]:
    return {x.strip().upper() for x in get_settings().leave_on_behalf_approvers.split(",") if x.strip()}


ON_BEHALF_BACKDATE_DAYS = 183


class OnBehalfIn(LeaveIn):
    staff_id: str


@router.post("/admin/leave/requests")
def record_for_someone(body: OnBehalfIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    """An admin records leave for a staff member (e.g. after the fact). It waits for Brendan's approval."""
    staff = db.get(Staff, body.staff_id)
    if staff is None or not staff.active:
        raise HTTPException(404, "No such staff member.")
    if staff.staff_id == admin.staff_id:
        raise HTTPException(400, "Use your own Leave form for your leave.")
    if not staff.has_leave:
        raise HTTPException(400, f"{staff.name} doesn't take leave in the app.")
    lt = type_by_code(staff, body.leave_type, _long_service_ok(db, staff), _is_casual(db, staff))
    if lt is None:
        raise HTTPException(400, f"That leave type isn't available to {staff.name}.")
    if body.end_date < body.start_date:
        raise HTTPException(400, "The end date is before the start date.")
    if (body.end_date - body.start_date).days >= MAX_REQUEST_DAYS:
        raise HTTPException(400, f"One request can cover at most {MAX_REQUEST_DAYS} days. Split it into two.")
    if len(body.reason.strip()) < 3:
        raise HTTPException(400, "Please give a reason.")
    if body.start_date < today() - timedelta(days=ON_BEHALF_BACKDATE_DAYS):
        raise HTTPException(400, "That's more than 6 months ago.")
    days = count_days(body.start_date, body.end_date, body.start_half, body.end_half)
    if days <= 0:
        raise HTTPException(400, "Those dates don't include any working days (Mon–Fri).")
    db.query(Staff).filter_by(staff_id=staff.staff_id).with_for_update().one()
    clash = (db.query(LeaveRequest)
             .filter(LeaveRequest.staff_id == staff.staff_id, LeaveRequest.status.in_(("pending", "approved")),
                     LeaveRequest.start_date <= body.end_date, LeaveRequest.end_date >= body.start_date).first())
    if clash:
        db.rollback()
        raise HTTPException(409, f"{staff.name} already has leave for {clash.start_date:%d %b}–{clash.end_date:%d %b}.")
    req = LeaveRequest(staff_id=staff.staff_id, leave_type=lt.code, start_date=body.start_date, end_date=body.end_date,
                       start_half=body.start_half, end_half=body.end_half, days=days, reason=body.reason.strip(),
                       status="pending", requested_by=admin.staff_id)
    db.add(req)
    db.flush()
    span = f"{_dates_text(req)} ({float(days):g} working day{'s' if days != 1 else ''})"
    for a in db.query(Staff).filter(Staff.staff_id.in_(on_behalf_approvers())):
        if a.email and a.staff_id != admin.staff_id:
            mailer.queue(db, a.email, f"Leave recorded for {staff.name} by {admin.name} — needs your approval",
                         f"{admin.name} recorded {lt.label.lower()} for {staff.name} ({staff.staff_id}):\n\n"
                         f"  {span}\n  Reason: {req.reason}\n\nOnly you can approve it — Leave tab in the staff app.")
    mailer.queue(db, staff.email, f"Leave recorded for you: {lt.label.lower()}, {_dates_text(req)}",
                 f"Hi {staff.name.split()[0]},\n\n{admin.name} has recorded {lt.label.lower()} for you:\n\n  {span}\n"
                 f"  Reason: {req.reason}\n\nIt's waiting for approval — you'll get an email when it's decided. "
                 "You can see it on the Leave tab in the staff app.")
    db.commit()
    return _request_out(req, staff, {admin.staff_id: admin.name})


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
    if req.requested_by and admin.staff_id not in on_behalf_approvers():
        raise HTTPException(403, "Leave recorded by an admin can only be approved by Brendan.")
    req.status = "approved" if body.decision == "approve" else "declined"
    req.decided_by = admin.staff_id
    req.decided_at = datetime.now(UTC)
    req.decision_note = body.note.strip()
    staff = db.get(Staff, req.staff_id)
    word = "approved" if req.status == "approved" else "declined"
    mailer.queue(db, staff.email, f"Your leave request was {word}",
                 f"Hi {staff.name.split()[0]},\n\n{admin.name} {word} your {label_for(staff, req.leave_type).lower()} for "
                 f"{_dates_text(req)}."
                 + (f"\n\nNote: {req.decision_note}" if req.decision_note else "")
                 + "\n\nYou can see your requests on the Leave tab in the staff app.")
    db.commit()
    return _request_out(req, staff)


# ---- attachments --------------------------------------------------------------------

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 5
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}


def sniff_type(data: bytes) -> str | None:
    """Real file type from its first bytes (never trust the name or the browser's claim)."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "image/heic"
    return None


def _safe_filename(name: str, content_type: str) -> str:
    import re
    from urllib.parse import unquote

    name = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", unquote(name or "")).strip(" ._") or "attachment"
    ext = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/heic": ".heic"}[content_type]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem[:120]}{ext}"


@router.post("/leave/requests/{request_id}/attachments")
async def add_attachment(request_id: int, request: Request, staff: Staff = Depends(ready_staff),
                         db: Session = Depends(get_db)):
    """Upload one file as the raw request body; its name goes in the X-Filename header."""
    import hashlib

    req = db.get(LeaveRequest, request_id)
    if req is None or not (req.staff_id == staff.staff_id or (staff.is_admin and req.requested_by == staff.staff_id)):
        raise HTTPException(404, "Request not found.")
    if req.status in ("cancelled", "declined"):
        raise HTTPException(409, f"That request was {req.status}, so files can't be added.")
    if db.query(LeaveAttachment).filter_by(request_id=req.id).count() >= MAX_ATTACHMENTS:
        raise HTTPException(409, f"A request can have at most {MAX_ATTACHMENTS} files.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, "That file is too big (10 MB maximum). Try a smaller photo or a PDF.")
    data = bytes(body)
    if not data:
        raise HTTPException(400, "The file is empty.")
    ctype = sniff_type(data)
    if ctype is None:
        raise HTTPException(415, "Only PDFs and photos (JPG, PNG, HEIC, WEBP) can be attached.")
    att = LeaveAttachment(request_id=req.id, filename=_safe_filename(request.headers.get("x-filename", ""), ctype),
                          content_type=ctype, size=len(data), sha256=hashlib.sha256(data).hexdigest(), data=data,
                          uploaded_by=staff.staff_id)
    db.add(att)
    db.commit()
    return {"id": att.id, "filename": att.filename, "content_type": ctype, "size": att.size}


@router.get("/leave/attachments/{attachment_id}")
def get_attachment(attachment_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    att = db.get(LeaveAttachment, attachment_id)
    req = db.get(LeaveRequest, att.request_id) if att else None
    if att is None or req is None or (req.staff_id != staff.staff_id and not staff.is_admin):
        raise HTTPException(404, "File not found.")
    inline = att.content_type in ("application/pdf", "image/jpeg", "image/png", "image/webp")
    return Response(att.data, media_type=att.content_type, headers={
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{att.filename}"',
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    })


@router.delete("/leave/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    att = db.get(LeaveAttachment, attachment_id)
    req = db.get(LeaveRequest, att.request_id) if att else None
    if att is None or req is None or req.staff_id != staff.staff_id:
        raise HTTPException(404, "File not found.")
    if req.status != "pending":
        raise HTTPException(409, "Files can only be removed while the request is waiting for approval.")
    db.delete(att)
    db.commit()
    return {"ok": True}
