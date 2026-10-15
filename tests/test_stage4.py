"""Stage 4: availability, roster clashes, emails and alerts."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import jobs, mailer, state
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_roster_service
from app.main import app
from app.models import EmailOutbox, LeaveRequest, Staff
from tests.conftest import HEADER, PASSWORD, add_staff, login

MEL = ZoneInfo("Australia/Melbourne")


def today():
    return datetime.now(MEL).date()


def next_weekday(wd: int, after: date | None = None) -> date:
    d = (after or today()) + timedelta(days=1)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


def roster(source, *rows):
    """rows: (date, staff_id, start, end)"""
    tabs = {}
    for d, sid, s, e in rows:
        tabs.setdefault(f"{d:%Y-%m}", [HEADER]).append([d.isoformat(), sid, sid.title(), "Fitness", "Class", s, e,
                                                        "AB HQ", "", "", ""])
    for m in (today(), today().replace(day=28) + timedelta(days=5)):
        tabs.setdefault(f"{m:%Y-%m}", [HEADER])
    source.tabs = tabs
    app.dependency_overrides[get_roster_service]()._snapshot = None


@pytest.fixture
def email_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "smtp_user", "hr@abgroup.au")
    monkeypatch.setattr(s, "smtp_password", "app-password")
    monkeypatch.setattr(s, "alert_email", "brendan@example.com")
    return s


def outbox():
    with SessionLocal() as db:
        return [(m.to_addr, m.subject, m.body) for m in db.query(EmailOutbox).order_by(EmailOutbox.id)]


def set_email(staff_id, email):
    with SessionLocal() as db:
        db.get(Staff, staff_id).email = email
        db.commit()


# ---- availability ----------------------------------------------------------------------

def test_weekly_and_dates_and_own_clashes(client, source):
    add_staff("AB0010", None)
    add_staff("AB0011", None)
    mon = next_weekday(0)
    wed = next_weekday(2)
    roster(source, (mon, "AB0010", "09:00", "12:00"), (wed, "AB0010", "13:00", "17:00"),
           (wed + timedelta(days=1), "AB0010", "09:00", "10:00"), (mon, "AB0011", "09:00", "12:00"))
    login(client, "AB0010")
    r = client.put("/api/availability/me/weekly", json={"days": [
        {"weekday": 0, "mode": "all_day"},
        {"weekday": 2, "mode": "window", "start": "06:00", "end": "14:00"},
    ]})
    assert r.status_code == 200
    thu = wed + timedelta(days=1)
    r = client.post("/api/availability/me/dates", json={"start_date": thu.isoformat(), "end_date": thu.isoformat(),
                                                        "start": "08:00", "end": "09:30", "note": "Dentist"})
    assert r.status_code == 200
    me = client.get("/api/availability/me").json()
    assert [d["mode"] for d in me["weekly"]] == ["all_day", "available", "window", "available", "available", "available", "available"]
    assert me["dates"][0]["note"] == "Dentist" and me["tz_label"] == "Melbourne time"
    reasons = {c["date"]: c["reason"] for c in me["clashes"]}
    assert reasons[mon.isoformat()] == "Not available Mondays (weekly)"
    assert reasons[wed.isoformat()].startswith("Not available Wednesdays 6am–2pm")  # 13:00 shift overlaps
    assert reasons[thu.isoformat()].startswith("Unavailable 8am–9:30am")
    assert all(c["staff_id"] == "AB0010" for c in me["clashes"])  # never anyone else's


def test_availability_validation_and_ownership(client, source):
    add_staff("AB0010", None)
    add_staff("AB0011", None)
    roster(source)
    login(client, "AB0011")
    d = (today() + timedelta(days=3)).isoformat()
    other = client.post("/api/availability/me/dates", json={"start_date": d, "end_date": d}).json()["id"]
    client.post("/api/auth/logout")
    login(client, "AB0010")
    bad = [
        {"start_date": d, "end_date": (today() + timedelta(days=1)).isoformat()},  # end before start
        {"start_date": "2020-01-01", "end_date": "2020-01-02"},  # past
        {"start_date": d, "end_date": d, "start": "10:00"},  # only one time
        {"start_date": d, "end_date": d, "start": "10:00", "end": "09:00"},
    ]
    for body in bad:
        assert client.post("/api/availability/me/dates", json=body).status_code == 400
    assert client.put("/api/availability/me/weekly", json={"days": [{"weekday": 1, "mode": "window"}]}).status_code == 400
    assert client.delete(f"/api/availability/me/dates/{other}").status_code == 404  # someone else's
    assert client.get("/api/admin/availability").status_code == 403


def test_admin_sees_everyone_and_leave_clashes(client, source):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None)
    d1, d2 = next_weekday(1), next_weekday(3)
    roster(source, (d1, "AB0010", "09:00", "17:00"), (d2, "AB0010", "09:00", "17:00"))
    with SessionLocal() as db:
        db.add(LeaveRequest(staff_id="AB0010", leave_type="annual", start_date=d1, end_date=d1, days=1,
                            reason="Trip", status="approved"))
        db.add(LeaveRequest(staff_id="AB0010", leave_type="personal", start_date=d2, end_date=d2, days=1,
                            reason="Appt", status="pending"))
        db.commit()
    login(client, "AB0001")
    a = client.get("/api/admin/availability").json()
    kinds = {c["date"]: (c["kind"], c["reason"]) for c in a["clashes"]}
    if d1 - today() < timedelta(days=14):
        assert kinds[d1.isoformat()] == ("leave", "Approved annual leave")
    if d2 - today() < timedelta(days=14):
        assert kinds[d2.isoformat()] == ("leave_pending", "Requested personal / carer's leave")
    assert {s["staff_id"] for s in a["staff"]} == {"AB0001", "AB0010"}


def test_offshore_availability_is_in_philippines_time(client, source):
    add_staff("AB0003", None)
    with SessionLocal() as db:
        db.get(Staff, "AB0003").location_type = "offshore"
        db.commit()
    tue = next_weekday(1)
    # 9am–6pm Melbourne = 6am–3pm Manila (AEDT, Oct–Apr) or 7am–4pm (AEST).
    roster(source, (tue, "AB0003", "09:00", "18:00"))
    login(client, "AB0003")
    client.put("/api/availability/me/weekly", json={"days": [{"weekday": 1, "mode": "window", "start": "17:00", "end": "23:00"}]})
    me = client.get("/api/availability/me").json()
    assert me["tz_label"] == "Philippines time" and me["clashes"] == []  # finishes before 5pm Manila
    client.put("/api/availability/me/weekly", json={"days": [{"weekday": 1, "mode": "window", "start": "05:00", "end": "07:00"}]})
    assert len(client.get("/api/availability/me").json()["clashes"]) == 1  # starts 6–7am Manila


# ---- email ------------------------------------------------------------------------------

class FakeSMTP:
    def __init__(self, fail_on=None):
        self.sent, self.fail_on = [], fail_on

    def send_message(self, msg):
        if self.fail_on and self.fail_on in msg["To"]:
            raise OSError("mailbox unavailable")
        self.sent.append(msg)

    def quit(self):
        pass


def test_leave_emails(client, source, email_on, monkeypatch):
    from app.api import leave as leave_api
    monkeypatch.setattr(leave_api, "today", lambda: date(2026, 10, 15))
    add_staff("AB0001", None, role="admin")
    add_staff("AB0002", None, role="admin")
    add_staff("AB0010", None)
    set_email("AB0001", "brendan@example.com")
    set_email("AB0010", "sonya@example.com")  # AB0002 has no email: skipped
    login(client, "AB0010")
    r = client.post("/api/leave/requests", json={"leave_type": "annual", "start_date": "2026-11-02",
                                                 "end_date": "2026-11-03", "reason": "Family wedding"})
    assert r.status_code == 200
    [(to, subject, body)] = outbox()
    assert to == "brendan@example.com" and "Ab0010" in subject and "Family wedding" in body
    client.post("/api/auth/logout")
    login(client, "AB0001")
    client.post(f"/api/admin/leave/{r.json()['id']}/decide", json={"decision": "approve", "note": "Enjoy"})
    to, subject, body = outbox()[-1]
    assert to == "sonya@example.com" and subject == "Your leave request was approved"
    assert "Mon 2 Nov 2026 to Tue 3 Nov 2026" in body and "Enjoy" in body

    fake = FakeSMTP(fail_on="sonya")
    with SessionLocal() as db:
        assert mailer.send_pending(db, connect=lambda: fake) == 1
    assert [m["To"] for m in fake.sent] == ["brendan@example.com"]
    with SessionLocal() as db:
        failed = db.query(EmailOutbox).filter_by(to_addr="sonya@example.com").one()
        assert failed.sent_at is None and failed.attempts == 1 and "mailbox unavailable" in failed.last_error
        # Retried later, not straight away.
        assert mailer.send_pending(db, connect=lambda: FakeSMTP()) == 0
        assert mailer.send_pending(db, now=datetime.now(UTC) + timedelta(minutes=3), connect=lambda: FakeSMTP()) == 1


def test_no_emails_when_not_set_up(client, source):
    add_staff("AB0001", None, role="admin")
    set_email("AB0001", "brendan@example.com")
    with SessionLocal() as db:
        assert mailer.queue(db, "x@example.com", "Hi", "Body") is False
        assert mailer.send_pending(db) == 0
    assert outbox() == []


def test_staff_set_own_email(client, source):
    add_staff("AB0010", None)
    add_staff("AB0011", "taken@example.com")
    login(client, "AB0010")
    assert client.post("/api/auth/email", json={"email": "me@example.com", "password": "wrong"}).status_code == 400
    assert client.post("/api/auth/email", json={"email": "Taken@Example.com", "password": PASSWORD}).status_code == 409
    assert client.post("/api/auth/email", json={"email": "not-an-email", "password": PASSWORD}).status_code == 400
    r = client.post("/api/auth/email", json={"email": " Me@Example.com ", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["email"] == "me@example.com"
    client.post("/api/auth/logout")
    assert login(client, "me@example.com").status_code == 200  # works as a login too


# ---- background jobs ------------------------------------------------------------------

def at_local(hour, day=None):
    d = day or today()
    return datetime(d.year, d.month, d.day, hour, 5, tzinfo=MEL).astimezone(UTC)


def test_daily_digest_once_and_only_when_needed(client, source, email_on):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None)
    set_email("AB0001", "brendan@example.com")
    service = app.dependency_overrides[get_roster_service]()
    roster(source)
    with SessionLocal() as db:
        jobs.tick(db, service, now=at_local(6))  # before 7am: nothing
        jobs.tick(db, service, now=at_local(7))  # nothing to report: no email
    assert outbox() == []

    tomorrow = today() + timedelta(days=1)
    roster(source, (tomorrow, "AB0010", "09:00", "17:00"))
    with SessionLocal() as db:
        from app.models import AvailabilityWeekly
        db.add(AvailabilityWeekly(staff_id="AB0010", weekday=tomorrow.weekday()))
        db.commit()
        state.put(db, "digest_day", "")
        db.commit()
        jobs.tick(db, service, now=at_local(7))
        jobs.tick(db, service, now=at_local(9))  # same day: not again
    [(to, subject, body)] = outbox()
    assert to == "brendan@example.com" and "daily check" in subject
    assert "Ab0010 — Not available" in body


def test_digest_lists_yesterdays_clock_problems(client, source, email_on):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None)
    set_email("AB0001", "brendan@example.com")
    yesterday = today() - timedelta(days=1)
    roster(source, (yesterday, "AB0010", "09:00", "17:00"))
    service = app.dependency_overrides[get_roster_service]()
    with SessionLocal() as db:
        jobs.tick(db, service, now=at_local(7))
    body = outbox()[0][2]
    assert "Ab0010 — rostered 09:00–17:00, didn't clock in" in body


def test_roster_and_backup_alerts(client, source, email_on, monkeypatch):
    monkeypatch.setattr(email_on, "digest_hour", -1)
    monkeypatch.setattr(email_on, "backup_drive_folder_id", "folder")
    roster(source)
    service = app.dependency_overrides[get_roster_service]()
    source.fail = True
    t0 = datetime.now(UTC)
    with SessionLocal() as db:
        service._snapshot = None
        jobs.tick(db, service, now=t0)
        jobs.tick(db, service, now=t0 + timedelta(minutes=10))
        assert outbox() == []
        jobs.tick(db, service, now=t0 + timedelta(minutes=31))
        jobs.tick(db, service, now=t0 + timedelta(minutes=40))  # once per day
    assert [s for _, s, _ in outbox()] == ["[AB app] Roster sheet can't be read"]

    source.fail = False
    with SessionLocal() as db:
        service._snapshot = None
        state.put(db, "last_offsite_backup", (t0 - timedelta(hours=20)).isoformat())
        db.commit()
        jobs.tick(db, service, now=t0 + timedelta(hours=1))
        assert len(outbox()) == 1
        jobs.tick(db, service, now=t0 + timedelta(hours=17))
    assert outbox()[-1][1] == "[AB app] No backup in the last 36 hours"


def test_offsite_upload_records_time(monkeypatch):
    from app import offsite
    offsite._record_success("roster-x.sql.gz.gpg")
    with SessionLocal() as db:
        assert state.get(db, "last_offsite_backup")


def test_cli_set_email_and_reseed_keeps_it(tmp_path):
    from app.cli import main, seed_staff
    add_staff("AB0010", None)
    assert main(["set-email", "AB0010", "Sonya@Example.com"]) == 0
    p = tmp_path / "staff.csv"
    p.write_text("staff_id,name,location_type\nAB0010,Sonya,onshore\n")
    assert seed_staff(str(p)) == 0
    with SessionLocal() as db:
        assert db.get(Staff, "AB0010").email == "sonya@example.com"
