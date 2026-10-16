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
        from app import payreport, travel
        locations = {sh.location for sh in snap.shifts
                     if sh.date >= today and payreport.is_first_aid(sh) and sh.location}
        new = sorted(v.label for v in travel.venues_for(db, locations).values() if v.kind == "unknown")
        if new:
            sections.append("First Aid venues to set up (campus or client site, so travel is paid correctly) — "
                            "Clock → Pay report → First Aid venues:\n" + "\n".join(f"• {n}" for n in new))

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
            subject = f"AB staff app — daily check, {local:%a %-d %b}"
            if s.digest_email.strip():
                for addr in (a.strip() for a in s.digest_email.split(",")):
                    if addr:
                        mailer.queue(db, addr, subject, body, f"digest:{today}:{addr}")
            else:
                mailer.queue_admins(db, subject, body, f"digest:{today}")
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
                send(db, payreport.build(db, service, last_end, now), now)
            except Exception as e:  # noqa: BLE001 — e.g. roster tab missing
                db.rollback()
                mailer.alert(db, "Pay report couldn't be made",
                             f"The pay report for the fortnight ending {last_end:%a %-d %b} failed: {e}\n"
                             "Open Clock → Pay report in the app to see it, once fixed.", f"pay:{last_end}")
            state.put(db, "pay_report_sent", last_end.isoformat())
            db.commit()

    availability_emails(db, local)
    mailer.send_pending(db, now)


REMINDER_DAYS = (1, 10, 14)
REMINDER_HOUR, SUMMARY_DAY, SUMMARY_HOUR = 9, 16, 7


def availability_emails(db: Session, local: datetime) -> None:
    """Monthly-availability reminders (1st, 10th, 14th) and the summary for the rosterer (16th)."""
    from app.models import AvailabilityMonth

    s = get_settings()
    today = local.date()
    nxt = av.next_month(today)
    label = av.month_label(nxt)
    people = [p for p in db.query(Staff).filter_by(active=True) if av.applies_to(db, p)]
    submitted = {m.staff_id for m in db.query(AvailabilityMonth).filter_by(month=nxt)}

    if today.day in REMINDER_DAYS and local.hour >= REMINDER_HOUR and state.get(db, "avail_reminder_day") != today.isoformat():
        last = today.day == REMINDER_DAYS[-1]
        for p in people:
            if p.availability_mode == "available" and p.staff_id not in submitted and p.email:
                mailer.queue(db, p.email, f"{'Last call: ' if last else ''}your availability for {label} is due by the 15th",
                             f"Hi {p.name.split()[0]},\n\nPlease enter the days and times you can work in {label} and "
                             f"press Submit, by {today:%B} 15 — open the staff app, My shifts → My availability."
                             + (" This is the last reminder." if last else ""),
                             f"avail-remind:{p.staff_id}:{today}")
        state.put(db, "avail_reminder_day", today.isoformat())
        db.commit()

    if today.day == SUMMARY_DAY and local.hour >= SUMMARY_HOUR and state.get(db, "avail_summary") != nxt:
        parts = []
        monthly = [p for p in people if p.availability_mode == "available"]
        if monthly:
            parts.append("MONTHLY AVAILABILITY (when they can work)")
            for p in monthly:
                rows = av.entries_for_month(db, p.staff_id, nxt, "available")
                head = f"{p.name} ({p.staff_id})" + ("" if p.staff_id in submitted else " — NOT SUBMITTED")
                parts.append(f"\n{head}\n{av.entries_text(rows)}")
        others = [p for p in people if p.availability_mode != "available"]
        unavail = [(p, av.entries_for_month(db, p.staff_id, nxt, "unavailable")) for p in others]
        unavail = [(p, rows) for p, rows in unavail if rows]
        if unavail:
            parts.append("\n\nUNAVAILABLE (days/times they can't work)")
            for p, rows in unavail:
                parts.append(f"\n{p.name} ({p.staff_id})\n{av.entries_text(rows)}")
        if parts:
            missing = [p.name for p in monthly if p.staff_id not in submitted]
            intro = f"Availability for {label}." + (f" Not submitted: {', '.join(missing)}." if missing else " Everyone has submitted.")
            mailer.queue(db, s.availability_email, f"Availability for {label}", intro + "\n\n" + "\n".join(parts),
                         f"avail-summary:{nxt}")
        state.put(db, "avail_summary", nxt)
        db.commit()


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
