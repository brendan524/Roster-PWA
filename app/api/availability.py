"""Availability: casual/contract staff mark dates and times they can't work;
admins see them and the roster clashes (including leave, for everyone)."""

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import availability as av
from app import mailer, state
from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, get_roster_service, ready_staff
from app.leave import OFFSHORE_TYPES, ONSHORE_TYPES
from app.models import AvailabilityDate, AvailabilityMonth, Staff
from app.roster import RosterService, RosterUnavailable

router = APIRouter(prefix="/api", tags=["availability"])

LEAVE_LABELS = {t.code: t.label for t in ONSHORE_TYPES + OFFSHORE_TYPES}
CLASH_DAYS = 14


def _parse_t(v: str | None, what: str) -> time | None:
    if v in (None, ""):
        return None
    try:
        return time.fromisoformat(v)
    except ValueError as e:
        raise HTTPException(400, f"{what} isn't a valid time.") from e


def _window(start: str | None, end: str | None) -> tuple[time | None, time | None]:
    s, e = _parse_t(start, "From time"), _parse_t(end, "To time")
    if (s is None) != (e is None):
        raise HTTPException(400, "Give both a from and a to time, or neither for the whole day.")
    if s is not None and s >= e:
        raise HTTPException(400, "The to time must be after the from time.")
    return s, e


def _shifts(service: RosterService) -> list:
    try:
        return service.get().shifts
    except RosterUnavailable:
        return []


def _tz_label(staff: Staff) -> str:
    return get_settings().offshore_timezone_label if staff.location_type == "offshore" else "Melbourne time"


def _months(today: date) -> list[str]:
    return [av.month_of(today), av.next_month(today)]


def _due(month: str) -> date:
    """Availability for a month is due by the 15th of the month before."""
    first, _ = av.month_bounds(month)
    prev = first - timedelta(days=1)
    return date(prev.year, prev.month, 15)


def _changed_key(staff_id: str, month: str) -> str:
    return f"avail_changed:{staff_id}:{month}"


def _notify_rosterer(db: Session, subject: str, body: str) -> None:
    mailer.queue(db, get_settings().availability_email, subject, body)


@router.get("/availability/me")
def my_availability(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db),
                    service: RosterService = Depends(get_roster_service)):
    if not av.applies_to(db, staff):
        return {"applies": False}
    today = datetime.now(av.staff_tz(staff)).date()
    mine = [s for s in _shifts(service) if s.staff_id == staff.staff_id]  # their own shifts only
    out = {
        "applies": True,
        "mode": staff.availability_mode,
        "today": today.isoformat(),
        "tz_label": _tz_label(staff),
        "clashes": [c.to_dict(staff.name) for c in
                    av.clashes(db, mine, today, today + timedelta(days=62), [staff.staff_id], LEAVE_LABELS)
                    if c.kind in ("date", "not_available")],
    }
    if staff.availability_mode == "available":
        subs = {m.month: m for m in db.query(AvailabilityMonth).filter_by(staff_id=staff.staff_id)}
        out["months"] = [{
            "month": m, "label": av.month_label(m), "due": _due(m).isoformat(),
            "submitted_at": subs[m].submitted_at.isoformat() if m in subs else None,
            "changed": bool(state.get(db, _changed_key(staff.staff_id, m))),
            "entries": av.dates_out(av.entries_for_month(db, staff.staff_id, m, "available")),
        } for m in _months(today)]
    else:
        out["dates"] = av.dates_out([d for d in av.upcoming_dates(db, staff.staff_id, today) if d.kind == "unavailable"])
    return out


class DatesIn(BaseModel):
    start_date: date
    end_date: date
    start: str | None = None
    end: str | None = None
    note: str = Field("", max_length=300)


@router.post("/availability/me/dates")
def add_dates(body: DatesIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    if not av.applies_to(db, staff):
        raise HTTPException(403, "Please apply for leave instead (Leave tab).")
    today = datetime.now(av.staff_tz(staff)).date()
    if body.end_date < body.start_date:
        raise HTTPException(400, "The end date is before the start date.")
    if body.end_date < today:
        raise HTTPException(400, "Those dates have already passed.")
    if (body.end_date - body.start_date).days >= av.MAX_RANGE_DAYS:
        raise HTTPException(400, f"One entry can cover at most {av.MAX_RANGE_DAYS} days.")
    s, e = _window(body.start, body.end)
    kind = staff.availability_mode
    if kind == "available" and not {av.month_of(body.start_date), av.month_of(body.end_date)} <= set(_months(today)):
        raise HTTPException(400, "Availability can be entered for this month and next month.")
    row = AvailabilityDate(staff_id=staff.staff_id, start_date=body.start_date, end_date=body.end_date,
                           start_time=s, end_time=e, note=body.note.strip(), kind=kind)
    db.add(row)
    db.flush()
    if kind == "unavailable":
        _notify_rosterer(db, f"Unavailable: {staff.name} — {av.entry_text(row)}",
                         f"{staff.name} ({staff.staff_id}) can't work:\n\n  {av.entry_text(row)}")
    else:
        _mark_changed(db, staff, row)
    db.commit()
    return av.dates_out([row])[0]


def _mark_changed(db: Session, staff: Staff, row: AvailabilityDate) -> None:
    for m in {av.month_of(row.start_date), av.month_of(row.end_date)}:
        if db.get(AvailabilityMonth, (staff.staff_id, m)):
            state.put(db, _changed_key(staff.staff_id, m), "1")


@router.delete("/availability/me/dates/{entry_id}")
def delete_dates(entry_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    row = db.query(AvailabilityDate).filter_by(id=entry_id, staff_id=staff.staff_id).first()
    if row is None:
        raise HTTPException(404, "Not found.")
    if row.kind == "unavailable":
        _notify_rosterer(db, f"No longer unavailable: {staff.name} — {av.entry_text(row)}",
                         f"{staff.name} ({staff.staff_id}) removed this — they can work then after all:\n\n"
                         f"  {av.entry_text(row)}")
    else:
        _mark_changed(db, staff, row)
    db.delete(row)
    db.commit()
    return {"ok": True}


class SubmitIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


@router.post("/availability/me/submit")
def submit_month(body: SubmitIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    if not av.applies_to(db, staff) or staff.availability_mode != "available":
        raise HTTPException(403, "Monthly availability isn't set up for you.")
    today = datetime.now(av.staff_tz(staff)).date()
    if body.month not in _months(today):
        raise HTTPException(400, "You can submit this month or next month.")
    sub = db.get(AvailabilityMonth, (staff.staff_id, body.month))
    again = sub is not None
    if sub is None:
        sub = AvailabilityMonth(staff_id=staff.staff_id, month=body.month)
        db.add(sub)
    sub.submitted_at = datetime.now().astimezone()
    state.put(db, _changed_key(staff.staff_id, body.month), "")
    rows = av.entries_for_month(db, staff.staff_id, body.month, "available")
    label = av.month_label(body.month)
    _notify_rosterer(db, f"{'Updated a' if again else 'A'}vailability for {label}: {staff.name}",
                     f"{staff.name} ({staff.staff_id}) {'updated their' if again else 'submitted their'} availability for "
                     f"{label} ({_tz_label(staff)}). They can work:\n\n{av.entries_text(rows)}")
    db.commit()
    return {"ok": True, "submitted_at": sub.submitted_at.isoformat()}


@router.get("/admin/availability")
def admin_availability(_: Staff = Depends(admin_staff), db: Session = Depends(get_db),
                       service: RosterService = Depends(get_roster_service)):
    today = service.today()
    start, end = av.horizon(today, CLASH_DAYS)
    everyone = db.query(Staff).filter_by(active=True).all()
    people = sorted((s for s in everyone if av.applies_to(db, s)), key=lambda s: s.name.lower())
    dates, _leave = av.load(db, [s.staff_id for s in people], today, today + timedelta(days=366))
    names = {s.staff_id: s.name for s in everyone}
    recent = datetime.now().astimezone() - timedelta(days=7)
    found = av.clashes(db, _shifts(service), start, end, None, LEAVE_LABELS)
    nxt = av.next_month(today)
    subs = {m.staff_id: m for m in db.query(AvailabilityMonth).filter_by(month=nxt)}
    return {
        "today": today.isoformat(),
        "days": CLASH_DAYS,
        "next_month": nxt, "next_label": av.month_label(nxt), "due": _due(nxt).isoformat(),
        "clashes": [c.to_dict(names.get(c.shift.staff_id, c.shift.staff_name)) for c in found],
        "staff": [{
            "staff_id": s.staff_id, "name": s.name, "tz_label": _tz_label(s), "mode": s.availability_mode,
            "next_submitted_at": subs[s.staff_id].submitted_at.isoformat() if s.staff_id in subs else None,
            "dates": [d | {"new": bool(d["created_at"]) and datetime.fromisoformat(d["created_at"]) >= recent}
                      for d in av.dates_out(dates.get(s.staff_id, []))],
        } for s in people],
    }


class ModeIn(BaseModel):
    mode: str = Field(pattern="^(available|unavailable)$")


@router.post("/admin/staff/{staff_id}/availability-mode")
def set_mode(staff_id: str, body: ModeIn, _: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    st = db.get(Staff, staff_id)
    if st is None:
        raise HTTPException(404, "No such staff member.")
    st.availability_mode = body.mode
    db.commit()
    return {"ok": True}
