"""Background jobs, run about once a minute:

- send queued emails;
- the daily admin digest (roster clashes for the next 14 days, yesterday's
  clock-in problems, roster sheet errors), only when there's something to say;
- system alerts to ALERT_EMAIL: roster sheet unreadable for 30+ minutes, and
  no off-site backup arriving for 36+ hours (e.g. the nightly job stopped).
  Xero and backup-script failures raise their own alerts where they happen.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import availability as av
from app import mailer, state
from app.config import get_settings
from app.models import Staff
from app.roster import RosterService, RosterUnavailable

log = logging.getLogger(__name__)

ROSTER_ALERT_AFTER = timedelta(minutes=30)
BACKUP_ALERT_AFTER = timedelta(hours=36)


def _fmt_day(d) -> str:
    return d.strftime("%a %-d %b")


def build_digest(db: Session, service: RosterService, now: datetime) -> str | None:
    from app.api.availability import CLASH_DAYS, LEAVE_LABELS
    from app.api.clock import day_report

    tz = ZoneInfo(get_settings().timezone)
    today = now.astimezone(tz).date()
    names = {s.staff_id: s.name for s in db.query(Staff)}
    try:
        snap = service.get()
    except RosterUnavailable:
        snap = None
    sections = []

    if snap is not None:
        start, end = av.horizon(today, CLASH_DAYS)
        found = av.clashes(db, snap.shifts, start, end, None, LEAVE_LABELS)
        if found:
            lines = [f"• {_fmt_day(c.shift.date)} {c.shift.start}–{c.shift.end}  "
                     f"{names.get(c.shift.staff_id, c.shift.staff_name)} — {c.reason}" for c in found]
            sections.append(f"Rostered while unavailable or on leave (next {CLASH_DAYS} days):\n" + "\n".join(lines))

    yesterday = today - timedelta(days=1)
    sessions, no_shows, _known, _err = day_report(db, service, yesterday, now)
    problems = []
    for s in sessions:
        for f in s.flags:
            if f["level"] == "error":
                problems.append(f"• {names.get(s.staff_id, s.staff_id)} — {f['label']}")
    for sh in no_shows:
        problems.append(f"• {names.get(sh.staff_id, sh.staff_name)} — rostered {sh.start}–{sh.end}, didn't clock in")
    if problems:
        sections.append(f"Clock-in problems on {_fmt_day(yesterday)} (fix on the Clock tab):\n" + "\n".join(problems))

    if snap is not None:
        errors = [i for i in snap.issues if i.level == "error"]
        if errors:
            sections.append(f"Roster sheet: {len(errors)} row(s) can't be shown to staff — see the Issues tab.")

    if not sections:
        return None
    return f"Good morning. Here's what needs a look today ({_fmt_day(today)}):\n\n" + "\n\n".join(sections)


def tick(db: Session, service: RosterService, now: datetime | None = None) -> None:
    s = get_settings()
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo(s.timezone))
    today = local.date().isoformat()

    # Daily digest, once per day after DIGEST_HOUR.
    if 0 <= s.digest_hour <= 23 and local.hour >= s.digest_hour and state.get(db, "digest_day") != today:
        body = build_digest(db, service, now)
        if body:
            mailer.queue_admins(db, f"AB staff app — daily check, {local:%a %-d %b}", body, f"digest:{today}")
        state.put(db, "digest_day", today)
        db.commit()

    # Roster sheet unreadable for a while?
    try:
        snap = service.get()
        failing = snap.error is not None
    except RosterUnavailable:
        failing = True
    since = state.get(db, "roster_failing_since")
    if failing:
        if not since:
            state.put(db, "roster_failing_since", now.isoformat())
        elif now - datetime.fromisoformat(since) >= ROSTER_ALERT_AFTER:
            mailer.alert(db, "Roster sheet can't be read",
                         "The app hasn't been able to read the Google Sheet roster for over 30 minutes. Staff see the "
                         "last copy it had. Check the sheet still exists and is shared with the app's service account.",
                         f"roster:{today}")
    elif since:
        state.put(db, "roster_failing_since", "")
    db.commit()

    # Off-site backups stopped arriving?
    if s.backup_drive_folder_id:
        last = state.get(db, "last_offsite_backup")
        started = state.get(db, "jobs_started_at")
        if not started:
            state.put(db, "jobs_started_at", now.isoformat())
        ref = last or started
        if ref and now - datetime.fromisoformat(ref) >= BACKUP_ALERT_AFTER:
            mailer.alert(db, "No backup in the last 36 hours",
                         "No off-server backup copy has arrived since "
                         + (datetime.fromisoformat(last).astimezone(local.tzinfo).strftime("%a %-d %b %H:%M") if last
                            else "the app started")
                         + ". Check the nightly backup on the server: tail /root/backups/ab-roster/backup.log",
                         f"backup-missing:{today}")
        db.commit()

    # Pay report: the Monday after each fortnight ends (within 2 days, so a late deploy doesn't send old ones).
    if 0 <= s.pay_report_hour <= 23 and local.hour >= s.pay_report_hour:
        from app import payreport
        from app.api.pay import send

        last_end = payreport.period_for(local.date())[0] - timedelta(days=1)
        if local.date() - last_end <= timedelta(days=2) and state.get(db, "pay_report_sent") != last_end.isoformat():
            try:
                send(db, payreport.build(db, service, last_end, now))
            except Exception as e:  # noqa: BLE001 — e.g. roster tab missing
                db.rollback()
                mailer.alert(db, "Pay report couldn't be made",
                             f"The pay report for the fortnight ending {last_end:%a %-d %b} failed: {e}\n"
                             "Open Clock → Pay report in the app to see it, once fixed.", f"pay:{last_end}")
            state.put(db, "pay_report_sent", last_end.isoformat())
            db.commit()

    mailer.send_pending(db, now)


def start_background(session_factory, service_factory) -> None:
    if not get_settings().jobs_enabled:
        return

    def loop():
        time.sleep(15)
        while True:
            try:
                with session_factory() as db:
                    tick(db, service_factory())
            except Exception:  # noqa: BLE001 — keep going; the next minute tries again
                log.exception("Background jobs failed")
            time.sleep(60)

    threading.Thread(target=loop, name="jobs", daemon=True).start()
