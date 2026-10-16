"""Monthly availability (e.g. Craig), unavailability (e.g. Jody), reminders and emails to the rosterer."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import availability as av
from app import jobs
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_roster_service
from app.main import app
from app.models import AvailabilityDate, AvailabilityMonth, EmailOutbox, Staff
from tests.conftest import HEADER, add_staff, login

MEL = ZoneInfo("Australia/Melbourne")


@pytest.fixture
def mail(monkeypatch):
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p"), ("availability_email", "dan@x.au")):
        monkeypatch.setattr(s, k, v)


def casual(sid, mode="unavailable", email=None):
    add_staff(sid, email)
    with SessionLocal() as db:
        s = db.get(Staff, sid)
        s.employment_status, s.availability_mode = "Casual", mode
        db.commit()


def emails(to=None):
    with SessionLocal() as db:
        q = db.query(EmailOutbox).order_by(EmailOutbox.id)
        return [(m.to_addr, m.subject, m.body) for m in q if to is None or m.to_addr == to]


def today():
    return datetime.now(MEL).date()


def next_month_day(day):
    t = today()
    return date(t.year + (t.month == 12), t.month % 12 + 1, day)


def test_unavailability_emails_dan_each_time(client, source, mail):
    casual("AB0016")
    login(client, "AB0016")
    d = (today() + timedelta(days=3)).isoformat()
    r = client.post("/api/availability/me/dates", json={"start_date": d, "end_date": d, "start": "09:00", "end": "12:00"})
    assert r.status_code == 200 and r.json()["kind"] == "unavailable"
    [(to, subject, body)] = emails()
    assert to == "dan@x.au" and subject.startswith("Unavailable: Ab0016") and "9am–12pm" in body
    client.delete(f"/api/availability/me/dates/{r.json()['id']}")
    assert emails()[-1][1].startswith("No longer unavailable: Ab0016")
    assert client.post("/api/availability/me/submit", json={"month": next_month_day(1).strftime("%Y-%m")}).status_code == 403


def test_monthly_availability_submit_and_update(client, source, mail):
    casual("AB0017", "available")
    login(client, "AB0017")
    me = client.get("/api/availability/me").json()
    assert me["mode"] == "available" and [m["submitted_at"] for m in me["months"]] == [None, None]
    nm = next_month_day(1).strftime("%Y-%m")
    assert me["months"][1]["due"] == date(today().year, today().month, 15).isoformat()
    for day, start, end in ((3, "08:00", "17:00"), (5, None, None)):
        d = next_month_day(day).isoformat()
        assert client.post("/api/availability/me/dates", json={"start_date": d, "end_date": d, "start": start, "end": end}).status_code == 200
    assert emails() == []  # nothing sent until they submit
    far = (today() + timedelta(days=80)).isoformat()
    assert client.post("/api/availability/me/dates", json={"start_date": far, "end_date": far}).status_code == 400
    assert client.post("/api/availability/me/submit", json={"month": nm}).status_code == 200
    [(to, subject, body)] = emails()
    assert to == "dan@x.au" and subject.startswith(f"Availability for {av.month_label(nm)}: Ab0017")
    assert "8am–5pm" in body and "all day" in body
    d = next_month_day(9).isoformat()
    client.post("/api/availability/me/dates", json={"start_date": d, "end_date": d})
    m = client.get("/api/availability/me").json()["months"][1]
    assert m["submitted_at"] and m["changed"] is True
    client.post("/api/availability/me/submit", json={"month": nm})
    assert emails()[-1][1].startswith("Updated availability")
    assert client.get("/api/availability/me").json()["months"][1]["changed"] is False


def test_monthly_clashes(client, source):
    casual("AB0017", "available")
    t = today()
    d1, d2, d3 = t + timedelta(days=1), t + timedelta(days=2), t + timedelta(days=3)
    months = {f"{d:%Y-%m}" for d in (t, d1, d2, d3)} | {av.next_month(t)}
    tabs = {m: [HEADER] for m in months}
    for d, s, e in ((d1, "09:00", "12:00"), (d2, "13:00", "18:00"), (d3, "09:00", "10:00")):
        tabs[f"{d:%Y-%m}"].append([d.isoformat(), "AB0017", "Craig", "First Aid", "Course", s, e, "Acme", "", "", ""])
    source.tabs = tabs
    with SessionLocal() as db:
        db.add_all([AvailabilityDate(staff_id="AB0017", start_date=d1, end_date=d1, kind="available"),
                    AvailabilityDate(staff_id="AB0017", start_date=d2, end_date=d2, kind="available",
                                     start_time=datetime.strptime("08:00", "%H:%M").time(),
                                     end_time=datetime.strptime("15:00", "%H:%M").time())])
        db.commit()
        service = app.dependency_overrides[get_roster_service]()
        assert av.clashes(db, service.get().shifts, t, t + timedelta(days=5)) == []  # month not submitted: no checks
        for m in {f"{d:%Y-%m}" for d in (d1, d2, d3)}:
            db.add(AvailabilityMonth(staff_id="AB0017", month=m))
        db.commit()
        found = {c.shift.date: c.reason for c in av.clashes(db, service.get().shifts, t, t + timedelta(days=5))}
    assert d1 not in found  # available all day
    assert found[d2] == "Only available 8am–3pm that day"
    assert found[d3] == "Not in their submitted availability"


def at(d, hour):
    return datetime(d.year, d.month, d.day, hour, 5, tzinfo=MEL)


def test_reminders_and_summary(client, source, mail):
    casual("AB0017", "available", "craig@x.au")
    casual("AB0020", "available", "kim@x.au")
    casual("AB0016", "unavailable", "jody@x.au")
    first = date(2026, 11, 1)
    nm = "2026-12"
    with SessionLocal() as db:
        db.add(AvailabilityMonth(staff_id="AB0020", month=nm))  # Kim already submitted
        db.add(AvailabilityDate(staff_id="AB0020", start_date=date(2026, 12, 2), end_date=date(2026, 12, 2), kind="available"))
        db.add(AvailabilityDate(staff_id="AB0016", start_date=date(2026, 12, 7), end_date=date(2026, 12, 7)))
        db.commit()
        jobs.availability_emails(db, at(first, 8))  # before 9am
        assert emails() == []
        jobs.availability_emails(db, at(first, 9))
        jobs.availability_emails(db, at(first, 15))  # once a day
        assert [(t, s) for t, s, _ in emails()] == [("craig@x.au", "your availability for December 2026 is due by the 15th")]
        jobs.availability_emails(db, at(date(2026, 11, 5), 9))  # not a reminder day
        jobs.availability_emails(db, at(date(2026, 11, 14), 9))
        assert emails()[-1][1].startswith("Last call:")
        jobs.availability_emails(db, at(date(2026, 11, 16), 7))
        jobs.availability_emails(db, at(date(2026, 11, 16), 12))
    summary = emails("dan@x.au")
    assert len(summary) == 1
    _, subject, body = summary[0]
    assert subject == "Availability for December 2026" and "Not submitted: Ab0017" in body
    assert "Ab0017 (AB0017) — NOT SUBMITTED" in body and "Wed 2 Dec, all day" in body and "Mon 7 Dec, all day" in body


def test_admin_sets_mode(client, source):
    add_staff("AB0001", None, role="admin")
    casual("AB0017")
    login(client, "AB0001")
    row = next(s for s in client.get("/api/admin/staff").json()["staff"] if s["staff_id"] == "AB0017")
    assert row["availability"] == "unavailable"
    assert next(s for s in client.get("/api/admin/staff").json()["staff"] if s["staff_id"] == "AB0001")["availability"] is None
    assert client.post("/api/admin/staff/AB0017/availability-mode", json={"mode": "available"}).status_code == 200
    with SessionLocal() as db:
        assert db.get(Staff, "AB0017").availability_mode == "available"
