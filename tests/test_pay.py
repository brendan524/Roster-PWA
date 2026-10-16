"""Stage 5: the fortnightly pay report."""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import jobs, payreport, state
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_roster_service
from app.main import app
from app.models import EmailOutbox, LeaveRequest, Punch, Staff, TravelLeg, Venue, XeroEmployeeLink
from tests.conftest import HEADER, add_staff, login

MEL = ZoneInfo("Australia/Melbourne")
END = date(2026, 10, 18)  # fortnight Mon 5 – Sun 18 Oct 2026
NOW = datetime(2026, 10, 19, 6, 0, tzinfo=MEL).astimezone(UTC)


def person(sid, status="Casual", location="onshore", **kw):
    add_staff(sid, None)
    with SessionLocal() as db:
        s = db.get(Staff, sid)
        s.employment_status, s.location_type = status, location
        for k, v in kw.items():
            setattr(s, k, v)
        db.commit()


def roster(source, *rows):
    """rows: (date, staff_id, start, end, dept, location, teaching)"""
    tabs = {}
    for d, sid, st, en, dept, loc, teach in rows:
        tabs.setdefault(f"{d:%Y-%m}", [HEADER]).append(
            [d.isoformat(), sid, sid.title(), dept, "Class", st, en, loc, "", "" if teach is None else teach, ""])
    for m in ("2026-10", "2026-11"):
        tabs.setdefault(m, [HEADER])
    source.tabs = tabs


def clock(sid, d, hhmm_in, hhmm_out=None):
    with SessionLocal() as db:
        for kind, hhmm in (("in", hhmm_in), ("out", hhmm_out)):
            if hhmm is None:
                continue
            h, m = map(int, hhmm.split(":"))
            db.add(Punch(client_id=str(uuid.uuid4()), staff_id=sid, kind=kind, source="auto", geo_status="auto",
                         occurred_at=datetime(d.year, d.month, d.day, h, m, tzinfo=MEL).astimezone(UTC)))
        db.commit()


def build():
    service = app.dependency_overrides[get_roster_service]()
    with SessionLocal() as db:
        rep = payreport.build(db, service, END, now=NOW)
        db.commit()
    return rep, {p["staff_id"]: p for p in rep["people"]}


def items(p):
    return {i["item"]: i["qty"] for i in p["items"]}


def test_periods_and_rounding():
    assert payreport.period_for(date(2026, 10, 15)) == (date(2026, 10, 5), date(2026, 10, 18))
    assert payreport.period_for(date(2026, 10, 4)) == (date(2026, 9, 21), date(2026, 10, 4))
    assert payreport.period_for(date(2026, 10, 5)) == (date(2026, 10, 5), date(2026, 10, 18))
    assert payreport.pay_day(END) == date(2026, 10, 21)
    assert [payreport.round15(h) for h in (2 + 7 / 60, 2 + 8 / 60, 0.1)] == [2.0, 2.25, 0.0]


def test_fitness_casual(source):
    person("AB0010")
    mon, tue, wed = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
    F = "Fitness"
    roster(source,
           (mon, "AB0010", "09:00", "12:00", F, "AB HQ", None),     # 20 min late → docked
           (tue, "AB0010", "06:00", "07:00", F, "Gym A", None),     # two shifts → broken shift
           (tue, "AB0010", "17:00", "17:45", F, "Gym B", None),     # 45 min → 1 h minimum
           (wed, "AB0010", "09:00", "10:00", F, "AB HQ", None),     # stayed 30 min longer: not paid
           (date(2026, 10, 8), "AB0010", "09:00", "10:00", F, "AB HQ", None))  # no-show
    clock("AB0010", mon, "09:20", "12:05")
    clock("AB0010", tue, "05:58", "07:01")
    clock("AB0010", tue, "17:00", "17:45")
    clock("AB0010", wed, "08:59", "10:30")
    _, by = build()
    p = by["AB0010"]
    assert items(p) == {"Casual Ordinary Hours": 2.75 + 1 + 1 + 1, "First Aid Allowance": 3,
                        "Broken Shift Allowance": 1, "Excess Fares Allowance": 1}
    text = " | ".join(p["flags"])
    assert "20 min late — 0.25 h docked" in text
    assert "31 min clocked outside the roster — not paid unless you approve" in text
    assert "no clock-in or clock-out" in text


def test_fitness_overtime_part_time_and_public_holiday(source):
    person("AB0007", "Part-time")
    days = [date(2026, 10, 5) + timedelta(days=i) for i in range(4)]
    rows = [(d, "AB0007", "07:00", "18:00", "Fitness", "AB HQ", None) for d in days]  # 11 h × 4
    rows.append((date(2026, 11, 3), "AB0007", "09:00", "10:00", "Fitness", "AB HQ", None))  # outside period
    roster(source, *rows)
    for d in days:
        clock("AB0007", d, "07:00", "18:00")
    _, by = build()
    it = items(by["AB0007"])
    assert it["Ordinary Hours"] == 38 and it["Overtime"] == 6  # 10/day ×3 = 30, then 8 to reach 38; rest OT


def test_public_holiday_hours(source, monkeypatch):
    monkeypatch.setattr(get_settings(), "extra_public_holidays", "2026-10-09")
    person("AB0010")
    d = date(2026, 10, 9)
    roster(source, (d, "AB0010", "09:00", "11:00", "Fitness", "AB HQ", None))
    clock("AB0010", d, "09:00", "11:00")
    _, by = build()
    assert items(by["AB0010"]) == {"Public Holiday Hours": 2, "First Aid Allowance": 1}


def test_first_aid(source):
    person("AB0018", "Casual")
    with SessionLocal() as db:
        db.add_all([Venue(name="tullamarine", label="Tullamarine", kind="campus"),
                    Venue(name="acme pty ltd", label="Acme Pty Ltd", kind="client", address="1 Client St, Geelong")])
        db.commit()
    monkey = get_settings()
    old = monkey.office_address
    monkey.office_address = "1 Office Rd, Tullamarine"
    try:
        with SessionLocal() as db:
            db.add(TravelLeg(origin="1 Office Rd, Tullamarine", destination="1 Client St, Geelong", km=80, minutes=62, source="manual"))
            db.add(TravelLeg(origin="1 Client St, Geelong", destination="1 Office Rd, Tullamarine", km=80, minutes=62, source="manual"))
            db.commit()
        mon, tue, sat = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 17)
        FA = "First Aid"
        roster(source,
               (mon, "AB0018", "08:30", "10:00", FA, "Tullamarine", 1.0),   # short course → 2 h teaching
               (mon, "AB0018", "10:30", "16:30", FA, "Tullamarine", 5.0),   # 2 + 5 = 7 → capped at 5
               (tue, "AB0018", "08:30", "16:30", FA, "Acme Pty Ltd", 6.0),  # client: travel; capped 5
               (sat, "AB0018", "09:00", "14:00", FA, "Unknown Hall", 4.0))  # past 12:30 Saturday; venue not set
        for d, a, b in ((mon, "08:25", "10:00"), (mon, "10:30", "16:35"), (tue, "08:20", "16:45"), (sat, "09:00", "14:00")):
            clock("AB0018", d, a, b)
        rep, by = build()
    finally:
        monkey.office_address = old
    p = by["AB0018"]
    it = items(p)
    assert it["Sessional Teaching"] == 5 + 5 + 4
    assert it["Non-Teaching Duties"] == 1.0 + 0.5 + 2.0 + 0.5  # buffers + 124 min travel (→2 h)
    assert it["Vehicle Allowance (cents per KM)"] == 160
    assert it["First Aid Allowance"] == 2 and it["Laundry Allowance"] == 2  # two calendar weeks
    text = " | ".join(p["flags"])
    assert "under 2 teaching hours" in text and "capped at the 5-hour daily rate" in text
    assert "outside ordinary hours" in text and "“Unknown Hall” isn't set" in text
    assert p["teaching_delivered"] == 1 + 5 + 6 + 4


def test_contractor_offshore_director_and_leave(source):
    person("AB0011", "Contract", has_leave=False)
    person("AB0003", "Contractor", location="offshore")
    person("AB0001", "Director")
    person("AB0016", "Casual")
    d = date(2026, 10, 6)
    roster(source, (d, "AB0011", "09:00", "11:00", "Fitness", "AB HQ", None),
           (d, "AB0003", "09:00", "18:00", "Offshore", "WrkPod", None),
           (d, "AB0001", "09:00", "17:00", "Admin", "AB HQ", None))
    clock("AB0011", d, "09:00", "11:10")
    clock("AB0003", d, "09:00", "18:00")
    clock("AB0001", d, "09:00", "17:00")
    with SessionLocal() as db:
        db.add(LeaveRequest(staff_id="AB0016", leave_type="unpaid", start_date=date(2026, 10, 15),
                            end_date=date(2026, 10, 20), days=4, reason="Trip", status="approved"))
        db.commit()
    rep, by = build()
    assert "AB0001" not in by
    assert by["AB0011"]["employment"] == "contractor" and by["AB0011"]["items"] == []
    assert by["AB0003"]["employment"] == "salaried" and items(by["AB0003"]) == {"Days worked": 1}
    assert by["AB0016"]["leave"] == ["Unpaid leave: 2 days (Thu 15 Oct – Sun 18 Oct)"]
    text = payreport.email_text(rep)
    assert "CONTRACTORS" in text and "Clocked 2.25 h over 1 shift(s) (rostered 2 h)" in text
    assert "OFFSHORE" in text


def test_xero_casual_beats_staff_file(source):
    person("AB0018", "Contract")  # staff file says Contract, but leave is on and Xero says casual
    with SessionLocal() as db:
        db.add(XeroEmployeeLink(staff_id="AB0018", xero_employee_id="E1", linked_by="auto", employment_basis="CASUAL"))
        db.commit()
    d = date(2026, 10, 6)
    roster(source, (d, "AB0018", "09:00", "11:00", "Fitness", "AB HQ", None))
    clock("AB0018", d, "09:00", "11:00")
    _, by = build()
    assert by["AB0018"]["employment"] == "casual" and items(by["AB0018"])["Casual Ordinary Hours"] == 2


def test_teaching_tracker_milestone(source):
    person("AB0018", teaching_hours_opening=795.0, teaching_hours_as_of=date(2026, 10, 4))
    d = date(2026, 10, 6)
    roster(source, (d, "AB0018", "09:00", "15:30", "First Aid", "", 6.0))
    clock("AB0018", d, "09:00", "15:30")
    rep, _ = build()
    [t] = rep["tracker"]
    assert t["total"] == 801 and t["reached"] == 800 and t["next_milestone"] == 1600
    assert "reached 800 hours" in payreport.email_text(rep)


def test_monday_email_once(client, source, monkeypatch):
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p"), ("alert_email", "brendan@x.au"), ("digest_hour", -1)):
        monkeypatch.setattr(s, k, v)
    person("AB0010")
    roster(source, (date(2026, 10, 6), "AB0010", "09:00", "11:00", "Fitness", "AB HQ", None))
    service = app.dependency_overrides[get_roster_service]()
    with SessionLocal() as db:
        jobs.tick(db, service, now=datetime(2026, 10, 19, 5, 0, tzinfo=MEL).astimezone(UTC))  # before 6am
        assert db.query(EmailOutbox).count() == 0
        jobs.tick(db, service, now=NOW)
        jobs.tick(db, service, now=NOW + timedelta(hours=3))
        [m] = db.query(EmailOutbox).all()
        assert m.to_addr == "brendan@x.au" and m.subject.startswith("Pay report 5 Oct – 18 Oct 2026")
        assert state.get(db, "pay_report_sent") == "2026-10-18"
        # A week later nothing old is re-sent.
        jobs.tick(db, service, now=NOW + timedelta(days=7))
        assert db.query(EmailOutbox).count() == 1


def test_api_admin_only_and_venues(client, source, monkeypatch):
    monkeypatch.setattr(get_settings(), "office_address", "1 Office Rd")
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None)
    d = date.today()
    roster(source, (d, "AB0010", "09:00", "11:00", "First Aid", "Acme", 1.5))
    login(client, "AB0010")
    assert client.get("/api/admin/pay").status_code == 403
    client.post("/api/auth/logout")
    login(client, "AB0001")
    r = client.get("/api/admin/pay")
    assert r.status_code == 200 and r.json()["current"] is True
    v = client.get("/api/admin/venues").json()["venues"]
    assert [x["label"] for x in v] == ["Acme"] and v[0]["kind"] == "unknown"
    r = client.post("/api/admin/venues", json={"name": "Acme", "kind": "client", "address": "5 Client St", "km": 30, "minutes": 25})
    assert r.status_code == 200 and r.json()["km"] == 30
    assert client.post("/api/admin/teaching-hours", json={"staff_id": "AB0010", "hours": 1650, "as_of": "2026-10-04"}).status_code == 200


def test_email_it_button(client, source, monkeypatch):
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p"), ("alert_email", "brendan@x.au")):
        monkeypatch.setattr(s, k, v)
    add_staff("AB0001", None, role="admin")
    roster(source)
    login(client, "AB0001")
    assert client.post("/api/admin/pay/email?end=2026-10-18").status_code == 200
    with SessionLocal() as db:
        [m] = db.query(EmailOutbox).all()
        assert m.to_addr == "brendan@x.au" and m.subject.startswith("Pay report 5 Oct – 18 Oct 2026")
    monkeypatch.setattr(s, "alert_email", "")
    r = client.post("/api/admin/pay/email?end=2026-10-18")
    assert r.status_code == 400 and "PAY_REPORT_EMAIL" in r.json()["detail"]


def test_queue_reports_success_and_dedupe():
    from app import mailer
    s = get_settings()
    old = (s.smtp_user, s.smtp_password)
    s.smtp_user, s.smtp_password = "hr@x.au", "p"
    try:
        with SessionLocal() as db:
            assert mailer.queue(db, "a@x.au", "Hi", "Body") is True
            assert mailer.queue(db, "a@x.au", "Hi", "Body", "k1") is True
            assert mailer.queue(db, "a@x.au", "Hi", "Body", "k1") is False  # same key: not again
            db.commit()
    finally:
        s.smtp_user, s.smtp_password = old
