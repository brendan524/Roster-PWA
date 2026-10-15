"""Clock in/out: recording punches safely, and turning them into shifts worked.

Pay-critical rules (see tests/test_punches.py):
- A punch is never lost: anything that reaches the server is stored, even if it
  looks odd (clocking in twice, clocking out without clocking in). Odd things
  are *flagged* for an admin, never silently dropped or "fixed".
- Re-sending the same punch (same client_id) is always safe: it's stored once.
- A double-tap (same person, same action within a minute) counts once.
- Punches are append-only. Corrections are new admin punches plus voids.
- All times are stored in UTC. Roster times are Melbourne wall-clock times and
  are converted per date (daylight-saving aware) before any comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Kiosk, Punch, PunchVoid, Staff
from app.roster import Shift
from app.security import hash_pin

MAX_QUEUE_AGE = timedelta(days=14)  # older queued punches are still stored, but flagged
CLOCK_SKEW_FLAG_S = 120  # device clock off by more than this is flagged
MATCH_WINDOW = timedelta(hours=4)  # how far a punch can be from a shift and still belong to it


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ms_to_dt(ms: float | None) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class PunchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---- PIN checks --------------------------------------------------------------

def check_own_pin(db: Session, staff: Staff, pin: str) -> None:
    """Personal device: the PIN must be the logged-in person's own. Raises PunchError."""
    s = get_settings()
    now = utcnow()
    if staff.pin_locked_until and staff.pin_locked_until > now:
        raise PunchError(429, "Too many wrong PINs. Try again in a few minutes.")
    if not staff.pin_hash:
        raise PunchError(400, "You don't have a PIN yet. Ask an admin to set one.")
    if hash_pin(pin) != staff.pin_hash:
        staff.pin_failed = (staff.pin_failed or 0) + 1
        if staff.pin_failed >= s.max_pin_failures:
            staff.pin_locked_until = now + timedelta(minutes=s.personal_pin_lock_minutes)
            staff.pin_failed = 0
        db.commit()
        raise PunchError(401, "That PIN isn't right.")
    if staff.pin_failed:
        staff.pin_failed = 0
        db.commit()


def staff_for_kiosk_pin(db: Session, kiosk: Kiosk, pin: str) -> Staff:
    """Kiosk: the PIN alone identifies the person. Repeated misses pause the kiosk briefly."""
    s = get_settings()
    now = utcnow()
    if kiosk.pin_locked_until and kiosk.pin_locked_until > now:
        raise PunchError(429, "Too many wrong PINs. Please wait a minute.")
    staff = db.query(Staff).filter_by(pin_hash=hash_pin(pin)).first() if pin else None
    if staff is None or not staff.active:
        kiosk.pin_failed = (kiosk.pin_failed or 0) + 1
        if kiosk.pin_failed >= s.max_pin_failures:
            kiosk.pin_locked_until = now + timedelta(seconds=s.kiosk_pin_lock_seconds)
            kiosk.pin_failed = 0
        db.commit()
        raise PunchError(401, "That PIN isn't right.")
    if kiosk.pin_failed:
        kiosk.pin_failed = 0
        db.commit()
    return staff


# ---- recording ---------------------------------------------------------------

@dataclass
class PunchResult:
    punch: Punch
    duplicate: bool  # True if this was a re-send or double-tap of an existing punch


def _voided_ids(db: Session, staff_id: str) -> set[int]:
    rows = db.execute(
        select(PunchVoid.punch_id).join(Punch, Punch.id == PunchVoid.punch_id).where(Punch.staff_id == staff_id)
    )
    return {r[0] for r in rows}


def record_punch(
    db: Session,
    *,
    staff: Staff,
    kind: str,
    source: str,
    client_id: str,
    device_time_ms: float | None = None,
    sent_at_ms: float | None = None,
    queued: bool = False,
    lat: float | None = None,
    lon: float | None = None,
    accuracy_m: float | None = None,
    geo_status: str = "unavailable",
    kiosk: Kiosk | None = None,
    occurred_at: datetime | None = None,  # admin entries only
    created_by: str | None = None,
    reason: str = "",
    user_agent: str = "",
    ip: str = "",
) -> PunchResult:
    if kind not in ("in", "out"):
        raise PunchError(400, "Unknown punch type.")
    if not client_id or len(client_id) > 64:
        raise PunchError(400, "Missing punch id.")

    existing = db.query(Punch).filter_by(client_id=client_id).first()
    if existing is not None:
        if existing.staff_id != staff.staff_id or existing.kind != kind:
            raise PunchError(409, "Punch id already used for a different punch.")
        return PunchResult(existing, duplicate=True)

    # Serialise punches per person so two simultaneous requests can't both pass the
    # double-tap check. Other people's punches aren't blocked.
    db.query(Staff).filter_by(staff_id=staff.staff_id).with_for_update().one()

    now = utcnow()
    device_time = _ms_to_dt(device_time_ms)
    sent_at = _ms_to_dt(sent_at_ms)
    offset_s = None
    if sent_at is not None:
        offset_s = round((now - sent_at).total_seconds())

    if occurred_at is None:
        if queued and device_time is not None:
            # Device clock corrected by how far it was off when it sent this punch.
            corrected = device_time + timedelta(seconds=offset_s or 0)
            occurred_at = min(corrected, now)  # never in the future
        else:
            occurred_at = now  # live punch: the server's clock is the authority

    if source != "admin":
        window = timedelta(seconds=get_settings().duplicate_window_seconds)
        voided = _voided_ids(db, staff.staff_id)
        recent = (
            db.query(Punch)
            .filter(Punch.staff_id == staff.staff_id, Punch.kind == kind,
                    Punch.occurred_at >= occurred_at - window, Punch.occurred_at <= occurred_at + window)
            .order_by(Punch.occurred_at.desc())
            .all()
        )
        recent = [p for p in recent if p.id not in voided]
        if recent:
            db.rollback()  # release the row lock
            return PunchResult(recent[0], duplicate=True)

    punch = Punch(
        client_id=client_id,
        staff_id=staff.staff_id,
        kind=kind,
        source=source,
        kiosk_id=kiosk.id if kiosk else None,
        occurred_at=occurred_at,
        received_at=now,
        device_time=device_time,
        clock_offset_s=offset_s,
        queued=bool(queued),
        lat=lat if geo_status == "ok" else None,
        lon=lon if geo_status == "ok" else None,
        accuracy_m=accuracy_m if geo_status == "ok" else None,
        geo_status=geo_status,
        created_by=created_by,
        reason=reason[:300],
        user_agent=user_agent[:255],
        ip=ip[:64],
    )
    db.add(punch)
    try:
        db.commit()
    except IntegrityError:
        # Same client_id inserted concurrently by another request: that one wins.
        db.rollback()
        existing = db.query(Punch).filter_by(client_id=client_id).first()
        if existing is None:
            raise
        return PunchResult(existing, duplicate=True)
    return PunchResult(punch, duplicate=False)


def void_punch(db: Session, punch: Punch, admin: Staff, reason: str) -> None:
    if not reason.strip():
        raise PunchError(400, "A reason is required.")
    if db.get(PunchVoid, punch.id):
        raise PunchError(409, "That punch is already removed.")
    db.add(PunchVoid(punch_id=punch.id, voided_by=admin.staff_id, reason=reason.strip()[:300]))
    db.commit()


def effective_punches(db: Session, staff_ids: list[str] | None, start: datetime, end: datetime) -> list[Punch]:
    q = db.query(Punch).filter(Punch.occurred_at >= start, Punch.occurred_at < end)
    if staff_ids is not None:
        q = q.filter(Punch.staff_id.in_(staff_ids))
    punches = q.order_by(Punch.occurred_at, Punch.id).all()
    if not punches:
        return []
    voided = {r[0] for r in db.execute(select(PunchVoid.punch_id).where(PunchVoid.punch_id.in_([p.id for p in punches])))}
    return [p for p in punches if p.id not in voided]


def current_status(db: Session, staff_id: str) -> Punch | None:
    """The person's open clock-in (if their latest effective punch is an 'in'), else None."""
    since = utcnow() - timedelta(days=3)
    punches = effective_punches(db, [staff_id], since, utcnow() + timedelta(minutes=1))
    if punches and punches[-1].kind == "in":
        return punches[-1]
    return None


# ---- sessions and flags --------------------------------------------------------

def shift_bounds(shift: Shift, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(shift.date, time.fromisoformat(shift.start), tz)
    end_date = shift.date + timedelta(days=1 if shift.overnight else 0)
    end = datetime.combine(end_date, time.fromisoformat(shift.end), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


@dataclass
class WorkSession:
    staff_id: str
    clock_in: Punch | None
    clock_out: Punch | None
    shift: Shift | None = None
    flags: list[dict[str, Any]] = field(default_factory=list)

    @property
    def anchor(self) -> datetime:
        return (self.clock_in or self.clock_out).occurred_at

    @property
    def worked_hours(self) -> float | None:
        if self.clock_in and self.clock_out:
            return round((self.clock_out.occurred_at - self.clock_in.occurred_at).total_seconds() / 3600, 2)
        return None


def pair_sessions(punches: list[Punch]) -> list[WorkSession]:
    """Pair one person's punches (time-ordered) into in→out sessions, keeping odd ones."""
    sessions: list[WorkSession] = []
    open_in: Punch | None = None
    for p in punches:
        if p.kind == "in":
            if open_in is not None:
                sessions.append(WorkSession(p.staff_id, open_in, None))  # never clocked out
            open_in = p
        else:
            if open_in is not None:
                sessions.append(WorkSession(p.staff_id, open_in, p))
                open_in = None
            else:
                sessions.append(WorkSession(p.staff_id, None, p))  # clock-out with no clock-in
    if open_in is not None:
        sessions.append(WorkSession(open_in.staff_id, open_in, None))
    return sessions


def _flag(code: str, label: str, level: str = "warning", **extra) -> dict[str, Any]:
    return {"code": code, "label": label, "level": level, **extra}


def analyse(
    sessions: list[WorkSession],
    shifts: list[Shift],
    tz: ZoneInfo,
    now: datetime,
    roster_known: bool = True,
) -> list[Shift]:
    """Match sessions to rostered shifts and add flags. Returns rostered shifts nobody clocked for."""
    s = get_settings()
    late_grace = timedelta(minutes=s.late_grace_minutes)
    early_grace = timedelta(minutes=s.early_grace_minutes)
    bounds = {id(sh): shift_bounds(sh, tz) for sh in shifts}
    unmatched = list(shifts)

    for sess in sorted(sessions, key=lambda x: x.anchor):
        mine = [sh for sh in unmatched if sh.staff_id == sess.staff_id]
        best, best_gap = None, None
        for sh in mine:
            start, end = bounds[id(sh)]
            ref = sess.clock_in.occurred_at if sess.clock_in else sess.clock_out.occurred_at
            target = start if sess.clock_in else end
            if start - MATCH_WINDOW <= ref <= end + MATCH_WINDOW:
                gap = abs(ref - target)
                if best_gap is None or gap < best_gap:
                    best, best_gap = sh, gap
        if best is not None:
            sess.shift = best
            unmatched.remove(best)

        if sess.shift is not None:
            start, end = bounds[id(sess.shift)]
            if sess.clock_in and sess.clock_in.occurred_at > start + late_grace:
                mins = round((sess.clock_in.occurred_at - start).total_seconds() / 60)
                sess.flags.append(_flag("late", f"Late {mins} min", minutes=mins))
            if sess.clock_out and sess.clock_out.occurred_at < end - early_grace:
                mins = round((end - sess.clock_out.occurred_at).total_seconds() / 60)
                sess.flags.append(_flag("early_finish", f"Left {mins} min early", minutes=mins))
            open_too_long = now > end + timedelta(hours=2)
        else:
            if roster_known:
                sess.flags.append(_flag("unrostered", "Not on the roster"))
            open_too_long = sess.clock_in is not None and now > sess.clock_in.occurred_at + timedelta(hours=12)

        if sess.clock_in is None:
            sess.flags.append(_flag("no_clock_in", "No clock-in", "error"))
        if sess.clock_out is None:
            if open_too_long:
                sess.flags.append(_flag("no_clock_out", "No clock-out", "error"))
            else:
                sess.flags.append(_flag("on_clock", "On the clock now", "info"))

        for p in (sess.clock_in, sess.clock_out):
            if p is None:
                continue
            word = "clock-in" if p.kind == "in" else "clock-out"
            if p.source == "personal" and p.geo_status != "ok":
                sess.flags.append(_flag("no_gps", f"No location on {word}", "info"))
            if p.queued:
                sess.flags.append(_flag("offline", f"{word.capitalize()} sent later (offline)", "info"))
                if p.received_at - p.occurred_at > MAX_QUEUE_AGE:
                    sess.flags.append(_flag("very_late_sync", f"{word.capitalize()} arrived over 14 days late"))
            if p.clock_offset_s is not None and abs(p.clock_offset_s) > CLOCK_SKEW_FLAG_S and p.queued:
                sess.flags.append(_flag("clock_adjusted",
                                        f"Phone clock was {abs(p.clock_offset_s) // 60} min off on {word}; corrected",
                                        "info"))
            if p.source == "admin":
                sess.flags.append(_flag("admin_entry", f"{word.capitalize()} entered by admin", "info"))

    no_shows = [sh for sh in unmatched if bounds[id(sh)][0] + late_grace < now]
    return no_shows


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tz)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tz)
    return start.astimezone(UTC), end.astimezone(UTC)
