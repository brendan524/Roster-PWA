"""Automatic clock-in/out for staff who don't clock themselves (e.g. directors).

Every few minutes, for each person marked auto_clock, the job looks at their
rostered shifts and records a clock-in at the rostered start and a clock-out at
the rostered end once those times have passed. The punches are ordinary
append-only rows with source "auto", so they show up (and can be corrected)
like any other. Each one has a fixed id built from the shift, so the job can
run any number of times without doubling up, and a punch an admin removes is
not put back.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Staff
from app.punches import record_punch, shift_bounds, utcnow
from app.roster import RosterService

log = logging.getLogger(__name__)

LOOKBACK = timedelta(hours=36)  # shifts that started this recently are (re)checked


def _client_id(staff_id: str, kind: str, at: datetime) -> str:
    return f"auto:{staff_id}:{at:%Y%m%dT%H%MZ}:{kind}"


def run(db: Session, service: RosterService, now: datetime | None = None) -> int:
    """Record any auto punches that are due. Returns how many were added."""
    now = now or utcnow()
    people = {s.staff_id: s for s in db.query(Staff).filter_by(auto_clock=True, active=True)}
    if not people:
        return 0
    snap = service.get()
    added = 0
    from app.leave import leave_on_days
    today = now.astimezone(service.tz).date()
    leave = leave_on_days(db, list(people), today - timedelta(days=2), today)
    for shift in sorted(snap.shifts, key=lambda s: (s.date, s.start)):
        staff = people.get(shift.staff_id)
        if staff is None:
            continue
        start, end = shift_bounds(shift, service.tz)
        if start > now or start < now - LOOKBACK:
            continue
        lv = leave.get((shift.staff_id, shift.date))
        if lv and lv["status"] == "approved" and not lv["half"]:
            continue  # on leave that day
        due = [("in", start)] + ([("out", end)] if end <= now else [])
        for kind, at in due:
            result = record_punch(db, staff=staff, kind=kind, source="auto", client_id=_client_id(staff.staff_id, kind, at),
                                  occurred_at=at, geo_status="auto", reason="Automatic (rostered times)")
            added += 0 if result.duplicate else 1
    return added


def start_background(session_factory, service_factory, minutes: float) -> None:
    if minutes <= 0:
        return

    def loop():
        time.sleep(20)
        while True:
            try:
                with session_factory() as db:
                    n = run(db, service_factory())
                    if n:
                        log.info("Auto clock: added %d punches", n)
            except Exception:  # noqa: BLE001 — roster unreachable etc.; try again next round
                log.exception("Auto clock failed")
            time.sleep(minutes * 60)

    threading.Thread(target=loop, name="auto-clock", daemon=True).start()
