from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, get_roster_service, ready_staff
from app.leave import leave_on_days
from app.models import Staff
from app.roster import RosterService, RosterSnapshot, RosterUnavailable, local_times, staff_issues

router = APIRouter(prefix="/api", tags=["roster"])


def _snapshot(service: RosterService, force: bool = False) -> RosterSnapshot:
    try:
        return service.get(force=force)
    except RosterUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


def _meta(service: RosterService, snap: RosterSnapshot) -> dict:
    return {
        "today": service.today().isoformat(),
        "fetched_at": snap.fetched_at.isoformat(),
        "stale": snap.error is not None,
    }


def _leave(db: Session, staff_ids: list[str] | None, snap: RosterSnapshot) -> dict:
    """Approved / requested leave on the days the roster covers, so shifts can show it."""
    days = [s.date for s in snap.shifts]
    return leave_on_days(db, staff_ids, min(days), max(days)) if days else {}


def _leave_list(db: Session, snap: RosterSnapshot, names: dict[str, str]) -> list[dict]:
    from datetime import date as _date, timedelta

    from app.leave import ONSHORE_TYPES, OFFSHORE_TYPES
    from app.models import LeaveRequest

    months = [m.name for m in snap.months]
    if not months:
        return []
    y, mo = map(int, months[0].split("-"))
    first = _date(y, mo, 1)
    y2, mo2 = map(int, months[-1].split("-"))
    last = _date(y2 + (mo2 == 12), mo2 % 12 + 1, 1) - timedelta(days=1)
    labels = {t.code: t.label for t in ONSHORE_TYPES + OFFSHORE_TYPES}
    rows = (db.query(LeaveRequest).filter(LeaveRequest.status.in_(("approved", "pending")),
                                          LeaveRequest.start_date <= last, LeaveRequest.end_date >= first)
            .order_by(LeaveRequest.start_date).all())
    return [{"staff_id": r.staff_id, "name": names.get(r.staff_id, r.staff_id), "status": r.status,
             "label": labels.get(r.leave_type, r.leave_type), "start_date": r.start_date.isoformat(),
             "end_date": r.end_date.isoformat(), "start_half": r.start_half, "end_half": r.end_half} for r in rows]


@router.get("/roster/me")
def my_roster(
    refresh: bool = False,
    staff: Staff = Depends(ready_staff),
    db: Session = Depends(get_db),
    service: RosterService = Depends(get_roster_service),
):
    """The logged-in person's own shifts ONLY. Never include other staff or sheet issues here."""
    snap = _snapshot(service, force=refresh)
    settings = get_settings()
    offshore = staff.location_type == "offshore"
    leave = _leave(db, [staff.staff_id], snap)

    def shift_out(s):
        d = s.to_dict() | ({"leave": leave[(s.staff_id, s.date)]} if (s.staff_id, s.date) in leave else {})
        if offshore:
            d["local"] = local_times(s, settings.timezone, settings.offshore_timezone)
        return d

    return {
        **_meta(service, snap),
        "sheet_tz_label": "Melbourne time" if settings.timezone == "Australia/Melbourne" else settings.timezone,
        "local_tz_label": settings.offshore_timezone_label if offshore else None,
        "months": [
            {
                "month": m.name,
                "published": m.exists,
                "shifts": [shift_out(s) for s in m.shifts if s.staff_id == staff.staff_id],
            }
            for m in snap.months
        ],
    }


@router.get("/admin/roster")
def all_roster(
    refresh: bool = False,
    _: Staff = Depends(admin_staff),
    db: Session = Depends(get_db),
    service: RosterService = Depends(get_roster_service),
):
    snap = _snapshot(service, force=refresh)
    staff_rows = db.query(Staff).all()
    leave = _leave(db, None, snap)
    known = {s.staff_id: s.active for s in staff_rows}
    issues = snap.issues + staff_issues(snap.shifts, known)
    issues.sort(key=lambda i: (i.level != "error", i.tab, i.row or 0))
    return {
        **_meta(service, snap),
        "refresh_error": snap.error,
        "months": [
            {"month": m.name, "published": m.exists,
             "shifts": [s.to_dict() | {"row": s.row} | ({"leave": leave[(s.staff_id, s.date)]}
                                                       if (s.staff_id, s.date) in leave else {}) for s in m.shifts]}
            for m in snap.months
        ],
        "issues": [i.to_dict() for i in issues],
        # Approved / requested leave in the months shown, for the team calendar.
        "leave": _leave_list(db, snap, {s.staff_id: s.name for s in staff_rows}),
        "staff": [
            {"staff_id": s.staff_id, "name": s.name, "department": s.department, "active": s.active}
            for s in sorted(staff_rows, key=lambda s: s.name.lower())
        ],
    }
