"""Availability: staff set when they can't work; admins see everyone's and the roster clashes."""

from datetime import date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import availability as av
from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, get_roster_service, ready_staff
from app.leave import ONSHORE_TYPES, OFFSHORE_TYPES
from app.models import AvailabilityDate, AvailabilityWeekly, Staff
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


@router.get("/availability/me")
def my_availability(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db),
                    service: RosterService = Depends(get_roster_service)):
    today = datetime.now(av.staff_tz(staff)).date()
    weekly = db.query(AvailabilityWeekly).filter_by(staff_id=staff.staff_id).all()
    start, end = today, today + timedelta(days=62)
    mine = [s for s in _shifts(service) if s.staff_id == staff.staff_id]
    st = get_settings()
    return {
        "today": today.isoformat(),
        "tz_label": st.offshore_timezone_label if staff.location_type == "offshore" else "Melbourne time",
        "weekly": av.weekly_out(weekly),
        "dates": av.dates_out(av.upcoming_dates(db, staff.staff_id, today)),
        # Their own shifts only (never anyone else's).
        "clashes": [c.to_dict(staff.name) for c in av.clashes(db, mine, start, end, [staff.staff_id], LEAVE_LABELS)
                    if c.kind in ("weekly", "date")],
    }


class DayIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    mode: Literal["available", "all_day", "window"]
    start: str | None = None
    end: str | None = None


class WeeklyIn(BaseModel):
    days: list[DayIn] = Field(max_length=7)


@router.put("/availability/me/weekly")
def set_weekly(body: WeeklyIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    if len({d.weekday for d in body.days}) != len(body.days):
        raise HTTPException(400, "Each day can only appear once.")
    rows = []
    for d in body.days:
        if d.mode == "available":
            continue
        s, e = _window(d.start, d.end) if d.mode == "window" else (None, None)
        if d.mode == "window" and s is None:
            raise HTTPException(400, f"Give the times you can't work on {av.DAYS[d.weekday]}.")
        rows.append(AvailabilityWeekly(staff_id=staff.staff_id, weekday=d.weekday, start_time=s, end_time=e))
    db.query(Staff).filter_by(staff_id=staff.staff_id).with_for_update().one()
    db.query(AvailabilityWeekly).filter_by(staff_id=staff.staff_id).delete()
    db.add_all(rows)
    db.commit()
    return {"weekly": av.weekly_out(rows)}


class DatesIn(BaseModel):
    start_date: date
    end_date: date
    start: str | None = None
    end: str | None = None
    note: str = Field("", max_length=300)


@router.post("/availability/me/dates")
def add_dates(body: DatesIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    today = datetime.now(av.staff_tz(staff)).date()
    if body.end_date < body.start_date:
        raise HTTPException(400, "The end date is before the start date.")
    if body.end_date < today:
        raise HTTPException(400, "Those dates have already passed.")
    if (body.end_date - body.start_date).days >= av.MAX_RANGE_DAYS:
        raise HTTPException(400, f"One entry can cover at most {av.MAX_RANGE_DAYS} days.")
    s, e = _window(body.start, body.end)
    row = AvailabilityDate(staff_id=staff.staff_id, start_date=body.start_date, end_date=body.end_date,
                           start_time=s, end_time=e, note=body.note.strip())
    db.add(row)
    db.commit()
    return av.dates_out([row])[0]


@router.delete("/availability/me/dates/{entry_id}")
def delete_dates(entry_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    row = db.query(AvailabilityDate).filter_by(id=entry_id, staff_id=staff.staff_id).first()
    if row is None:
        raise HTTPException(404, "Not found.")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/admin/availability")
def admin_availability(_: Staff = Depends(admin_staff), db: Session = Depends(get_db),
                       service: RosterService = Depends(get_roster_service)):
    today = service.today()
    start, end = av.horizon(today, CLASH_DAYS)
    people = sorted(db.query(Staff).filter_by(active=True), key=lambda s: s.name.lower())
    weekly, dates, _leave = av.load(db, None, today, today + timedelta(days=366))
    names = {s.staff_id: s.name for s in people}
    recent = datetime.now().astimezone() - timedelta(days=7)
    found = av.clashes(db, _shifts(service), start, end, None, LEAVE_LABELS)
    return {
        "today": today.isoformat(),
        "days": CLASH_DAYS,
        "clashes": [c.to_dict(names.get(c.shift.staff_id, c.shift.staff_name)) for c in found],
        "staff": [{
            "staff_id": s.staff_id, "name": s.name,
            "tz_label": get_settings().offshore_timezone_label if s.location_type == "offshore" else "Melbourne time",
            "summary": av.summary_text(weekly.get(s.staff_id, [])),
            "weekly": av.weekly_out(weekly.get(s.staff_id, [])),
            "dates": [d | {"new": bool(d["created_at"]) and datetime.fromisoformat(d["created_at"]) >= recent}
                      for d in av.dates_out(dates.get(s.staff_id, []))],
        } for s in people],
    }
