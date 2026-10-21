"""Clock in/out endpoints: personal device, kiosk, and the admin day view."""

import hashlib
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import authenticate
from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, get_roster_service, ready_staff
from app.leave import leave_on_days
from app.models import Kiosk, Punch, Staff
from app.punches import (
    PunchError,
    WorkSession,
    analyse,
    check_own_pin,
    current_status,
    effective_punches,
    local_day_bounds,
    pair_sessions,
    record_punch,
    staff_for_kiosk_pin,
    utcnow,
    void_punch,
)
from app.roster import RosterService, RosterUnavailable, month_tab

router = APIRouter(prefix="/api", tags=["clock"])


def _raise(e: PunchError):
    raise HTTPException(e.status, e.message) from e


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))


class Geo(BaseModel):
    status: Literal["ok", "denied", "unavailable", "timeout"] = "unavailable"
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    accuracy: float | None = Field(None, ge=0)


class PunchIn(BaseModel):
    client_id: str = Field(min_length=8, max_length=64)
    kind: Literal["in", "out"]
    pin: str = Field(max_length=12)
    device_time: float | None = None  # ms since epoch, device clock, when the button was pressed
    sent_at: float | None = None  # ms since epoch, device clock, when this request was sent
    queued: bool = False
    geo: Geo = Geo()


def _punch_reply(result, staff: Staff, db: Session) -> dict:
    open_in = current_status(db, staff.staff_id)
    return {
        "ok": True,
        "duplicate": result.duplicate,
        "kind": result.punch.kind,
        "occurred_at": _iso(result.punch.occurred_at),
        "name": staff.name.split()[0],
        "clocked_in": open_in is not None,
        "since": _iso(open_in.occurred_at) if open_in else None,
    }


# ---- personal device ----------------------------------------------------------

@router.get("/clock/me")
def my_clock(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    open_in = current_status(db, staff.staff_id)
    since = utcnow() - timedelta(days=14)
    recent = effective_punches(db, [staff.staff_id], since, utcnow() + timedelta(minutes=1))[-20:]
    return {
        "has_pin": bool(staff.pin_hash),
        "clocked_in": open_in is not None,
        "since": _iso(open_in.occurred_at) if open_in else None,
        "recent": [{"kind": p.kind, "occurred_at": _iso(p.occurred_at), "source": p.source}
                   for p in reversed(recent)],
    }


@router.post("/clock/punch")
def personal_punch(body: PunchIn, request: Request, staff: Staff = Depends(ready_staff),
                   db: Session = Depends(get_db)):
    try:
        check_own_pin(db, staff, body.pin)
        result = record_punch(
            db, staff=staff, kind=body.kind, source="personal", client_id=body.client_id,
            device_time_ms=body.device_time, sent_at_ms=body.sent_at, queued=body.queued,
            lat=body.geo.lat, lon=body.geo.lon, accuracy_m=body.geo.accuracy, geo_status=body.geo.status,
            user_agent=request.headers.get("user-agent", ""), ip=_client_ip(request),
        )
    except PunchError as e:
        _raise(e)
    return _punch_reply(result, staff, db)


# ---- kiosk --------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def kiosk_locations() -> list[dict]:
    out = []
    for item in get_settings().kiosk_locations.split(","):
        name, _, loc = item.partition(":")
        if name.strip() and loc.strip() in ("onshore", "offshore"):
            out.append({"name": name.strip(), "location_type": loc.strip()})
    return out


def _tz_for(location_type: str) -> str:
    s = get_settings()
    return s.offshore_timezone if location_type == "offshore" else s.timezone


def current_kiosk(x_kiosk_token: str | None = Header(None), db: Session = Depends(get_db)) -> Kiosk:
    kiosk = db.query(Kiosk).filter_by(token_hash=_token_hash(x_kiosk_token)).first() if x_kiosk_token else None
    if kiosk is None or not kiosk.active:
        raise HTTPException(401, "This kiosk isn't set up (or was removed). An admin needs to set it up again.")
    return kiosk


class KioskSetupIn(BaseModel):
    login: str
    password: str
    location: str


@router.get("/kiosk/locations")
def list_kiosk_locations():
    return kiosk_locations()


@router.post("/kiosk/setup")
def setup_kiosk(body: KioskSetupIn, db: Session = Depends(get_db)):
    admin = authenticate(db, body.login, body.password)
    if not admin.is_admin:
        raise HTTPException(403, "Only an admin can set up a kiosk.")
    loc = next((l for l in kiosk_locations() if l["name"] == body.location), None)
    if loc is None:
        raise HTTPException(400, "Unknown location.")
    token = secrets.token_urlsafe(32)
    kiosk = Kiosk(name=loc["name"], location_type=loc["location_type"], token_hash=_token_hash(token),
                  created_by=admin.staff_id, active=True, pin_failed=0)
    db.add(kiosk)
    db.commit()
    return {"token": token, "name": kiosk.name, "location_type": kiosk.location_type,
            "tz": _tz_for(kiosk.location_type)}


@router.get("/kiosk/me")
def kiosk_me(kiosk: Kiosk = Depends(current_kiosk), db: Session = Depends(get_db)):
    kiosk.last_seen_at = utcnow()
    db.commit()
    return {"name": kiosk.name, "location_type": kiosk.location_type, "tz": _tz_for(kiosk.location_type),
            "server_time": _iso(utcnow())}


@router.post("/kiosk/punch")
def kiosk_punch(body: PunchIn, request: Request, kiosk: Kiosk = Depends(current_kiosk),
                db: Session = Depends(get_db)):
    try:
        staff = staff_for_kiosk_pin(db, kiosk, body.pin)
        result = record_punch(
            db, staff=staff, kind=body.kind, source="kiosk", client_id=body.client_id, kiosk=kiosk,
            device_time_ms=body.device_time, sent_at_ms=body.sent_at, queued=body.queued, geo_status="kiosk",
            user_agent=request.headers.get("user-agent", ""), ip=_client_ip(request),
        )
    except PunchError as e:
        _raise(e)
    kiosk.last_seen_at = utcnow()
    db.commit()
    return _punch_reply(result, staff, db)


# ---- admin --------------------------------------------------------------------

def _punch_out(p: Punch | None, kiosks: dict[int, str]) -> dict | None:
    if p is None:
        return None
    return {
        "id": p.id,
        "occurred_at": _iso(p.occurred_at),
        "source": p.source,
        "kiosk": kiosks.get(p.kiosk_id) if p.kiosk_id else None,
        "geo": {"status": p.geo_status, "lat": p.lat, "lon": p.lon, "accuracy": p.accuracy_m},
        "queued": p.queued,
        "reason": p.reason or None,
        "created_by": p.created_by,
    }


def _session_out(sess: WorkSession, names: dict[str, str], kiosks: dict[int, str]) -> dict:
    sh = sess.shift
    return {
        "staff_id": sess.staff_id,
        "name": names.get(sess.staff_id, sess.staff_id),
        "clock_in": _punch_out(sess.clock_in, kiosks),
        "clock_out": _punch_out(sess.clock_out, kiosks),
        "worked_hours": sess.worked_hours,
        "shift": sh.to_dict() if sh else None,
        "flags": sess.flags,
    }


def day_report(db: Session, service: RosterService, day: date, now: datetime):
    """Clock sessions for one local day, matched to the roster with flags, plus rostered no-shows."""
    tz = ZoneInfo(get_settings().timezone)
    day_start, day_end = local_day_bounds(day, tz)

    # Punches near the day (so overnight shifts pair up), grouped per person.
    punches = effective_punches(db, None, day_start - timedelta(hours=16), day_end + timedelta(hours=16))
    by_staff: dict[str, list[Punch]] = {}
    for p in punches:
        by_staff.setdefault(p.staff_id, []).append(p)
    sessions = [s for ps in by_staff.values() for s in pair_sessions(ps)]

    shifts, roster_known, roster_error = [], False, None
    try:
        snap = service.get()
        near = {day - timedelta(days=1), day, day + timedelta(days=1)}
        shifts = [s for s in snap.shifts if s.date in near]
        roster_known = any(m.name == month_tab(day) and m.exists for m in snap.months)
    except RosterUnavailable as e:
        roster_error = str(e)

    no_shows = analyse(sessions, shifts, tz, now, roster_known=roster_known)
    todays = sorted((s for s in sessions if day_start <= s.anchor < day_end), key=lambda s: s.anchor)
    # Rostered but on approved leave that day: not a no-show.
    on_leave = {k for k, v in leave_on_days(db, None, day, day).items() if v["status"] == "approved" and not v["half"]}
    no_shows = [s for s in no_shows if s.date == day and (s.staff_id, s.date) not in on_leave]
    return todays, no_shows, roster_known, roster_error


@router.get("/admin/clock")
def admin_day(
    day: date | None = None,
    _: Staff = Depends(admin_staff),
    db: Session = Depends(get_db),
    service: RosterService = Depends(get_roster_service),
):
    tz = ZoneInfo(get_settings().timezone)
    now = utcnow()
    day = day or now.astimezone(tz).date()
    todays, no_shows, roster_known, roster_error = day_report(db, service, day, now)

    staff_rows = db.query(Staff).all()
    names = {s.staff_id: s.name for s in staff_rows}
    kiosks = {k.id: k.name for k in db.query(Kiosk).all()}

    # Who is clocked in right now (any day).
    recent = effective_punches(db, None, now - timedelta(days=3), now + timedelta(minutes=1))
    last: dict[str, Punch] = {}
    for p in recent:
        last[p.staff_id] = p
    on_clock = sorted(({"staff_id": sid, "name": names.get(sid, sid), "since": _iso(p.occurred_at)}
                       for sid, p in last.items() if p.kind == "in"), key=lambda x: x["since"])

    return {
        "day": day.isoformat(),
        "today": now.astimezone(tz).date().isoformat(),
        "tz": get_settings().timezone,
        "roster_known": roster_known,
        "roster_error": roster_error,
        "on_clock": on_clock,
        "sessions": [_session_out(s, names, kiosks) for s in todays],
        "no_shows": [s.to_dict() | {"name": names.get(s.staff_id, s.staff_name or s.staff_id)}
                     for s in no_shows],
        "staff": [{"staff_id": s.staff_id, "name": s.name} for s in sorted(staff_rows, key=lambda s: s.name)
                  if s.active],
    }


class AdminPunchIn(BaseModel):
    staff_id: str
    kind: Literal["in", "out"]
    local_time: datetime  # wall-clock time in the app time zone (Melbourne), no offset
    reason: str = Field(min_length=3, max_length=300)


@router.post("/admin/clock/punch")
def admin_add_punch(body: AdminPunchIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    staff = db.get(Staff, body.staff_id.upper())
    if staff is None:
        raise HTTPException(404, "No such staff member.")
    tz = ZoneInfo(get_settings().timezone)
    when = body.local_time.replace(tzinfo=tz) if body.local_time.tzinfo is None else body.local_time
    if when.astimezone(UTC) > utcnow() + timedelta(minutes=5):
        raise HTTPException(400, "That time is in the future.")
    try:
        result = record_punch(db, staff=staff, kind=body.kind, source="admin", client_id=f"admin-{uuid.uuid4()}",
                              occurred_at=when.astimezone(UTC), geo_status="admin",
                              created_by=admin.staff_id, reason=body.reason.strip())
    except PunchError as e:
        _raise(e)
    return {"ok": True, "id": result.punch.id}


class VoidIn(BaseModel):
    punch_id: int
    reason: str = Field(min_length=3, max_length=300)


@router.post("/admin/clock/void")
def admin_void(body: VoidIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    punch = db.get(Punch, body.punch_id)
    if punch is None:
        raise HTTPException(404, "No such punch.")
    try:
        void_punch(db, punch, admin, body.reason)
    except PunchError as e:
        _raise(e)
    return {"ok": True}
