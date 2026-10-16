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
    person("AB0018", "Casual", travel_eligible=True)
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
    assert it["Non-Teaching Duties"] == round(1.0 + 0.5 + 2.07 + 0.5, 2)  # buffers + 124 min travel (2.07 h)
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
    assert t["total"] == 801 and t["reached"] == 800 and t["next_milestone"] == 1600 and t["level"] == 2
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


def test_campus_patterns_and_travel_eligibility(source, monkeypatch):
    monkeypatch.setattr(get_settings(), "office_address", "Unit 2, 7-9 Butler Way, Tullamarine VIC 3043")
    person("AB0016", "Casual", travel_eligible=True)   # Jody
    person("AB0019", "Casual")                         # another trainer: no travel paid
    with SessionLocal() as db:
        db.add(Venue(name="acme", label="Acme", kind="client", address="1 Client St"))
        for a, b in (("Unit 2, 7-9 Butler Way, Tullamarine VIC 3043", "1 Client St"), ("1 Client St", "Unit 2, 7-9 Butler Way, Tullamarine VIC 3043")):
            db.add(TravelLeg(origin=a, destination=b, km=25.25, minutes=31, source="manual"))
        db.commit()
    mon, tue = date(2026, 10, 5), date(2026, 10, 6)
    FA = "First Aid"
    roster(source,
           (mon, "AB0016", "09:00", "12:00", FA, "Galactic Dance and Events", 2.5),
           (mon, "AB0019", "09:00", "12:00", FA, "Tullamarine HQ", 2.5),
           (tue, "AB0016", "09:00", "12:00", FA, "Acme", 2.5),
           (tue, "AB0016", "13:00", "16:00", FA, "Acme", 2.5),   # same client twice: one trip
           (tue, "AB0019", "09:00", "12:00", FA, "Acme", 2.5))
    for sid, d, a, b in (("AB0016", mon, "09:00", "12:00"), ("AB0019", mon, "09:00", "12:00"), ("AB0016", tue, "09:00", "12:00"),
                         ("AB0016", tue, "13:00", "16:00"), ("AB0019", tue, "09:00", "12:30")):
        clock(sid, d, a, b)
    _, by = build()
    with SessionLocal() as db:
        assert {v.label: v.kind for v in db.query(Venue)} == {"Acme": "client", "Galactic Dance and Events": "campus", "Tullamarine HQ": "campus"}
    jody, other = items(by["AB0016"]), items(by["AB0019"])
    assert jody["Vehicle Allowance (cents per KM)"] == 50.5 and jody["Non-Teaching Duties"] == round(1.5 + 62 / 60, 2)
    assert jody["Sessional Teaching"] == 7.5 and "Vehicle Allowance (cents per KM)" not in other
    assert other["Non-Teaching Duties"] == 1.0
    assert "5 teaching hours — the full daily rate" in " | ".join(by["AB0016"]["flags"])
    assert "clocked 30 min longer than the rostered shift" in " | ".join(by["AB0019"]["flags"])


def test_admin_travel_toggle(client, source):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0016", None)
    login(client, "AB0001")
    assert client.post("/api/admin/staff/AB0016/travel", json={"on": True}).status_code == 200
    row = next(s for s in client.get("/api/admin/staff").json()["staff"] if s["staff_id"] == "AB0016")
    assert row["travel_eligible"] is True


def test_level_up_email_once(source, monkeypatch):
    from app.api.pay import send
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p"), ("alert_email", "brendan@x.au")):
        monkeypatch.setattr(s, k, v)
    person("AB0018", teaching_hours_opening=1595.0, teaching_hours_as_of=date(2026, 10, 4))
    d = date(2026, 10, 6)
    roster(source, (d, "AB0018", "09:00", "15:30", "First Aid", "Tullamarine", 6.0))
    clock("AB0018", d, "09:00", "15:30")
    rep, _ = build()
    with SessionLocal() as db:
        send(db, rep, NOW)
        send(db, rep, NOW)  # e.g. "Email it" pressed again: no second level-up email
        db.commit()
        subjects = [m.subject for m in db.query(EmailOutbox)]
    assert subjects.count("Level up: Ab0018 has reached 1,600 teaching hours") == 1


def test_digest_lists_new_venues(client, source, monkeypatch):
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p")):
        monkeypatch.setattr(s, k, v)
    add_staff("AB0001", "brendan@x.au", role="admin")
    add_staff("AB0018", None)
    t = datetime.now(MEL).date() + timedelta(days=2)
    tabs = {m: [HEADER] for m in {f"{datetime.now(MEL).date():%Y-%m}", f"{t:%Y-%m}"}}
    tabs[f"{t:%Y-%m}"] += [[t.isoformat(), "AB0018", "Judy", "First Aid", "CPR", "09:00", "12:00", "Acme Pty Ltd", "", "2", ""],
                           [t.isoformat(), "AB0018", "Judy", "First Aid", "CPR", "13:00", "15:00", "Melton - Galactic", "", "2", ""]]
    source.tabs = tabs
    service = app.dependency_overrides[get_roster_service]()
    with SessionLocal() as db:
        body = jobs.build_digest(db, service, datetime.now(UTC))
    assert "First Aid venues to set up" in body and "• Acme Pty Ltd" in body and "Galactic" not in body


def test_first_aid_lateness_docked_and_long_day_flag(source, monkeypatch):
    monkeypatch.setattr(get_settings(), "office_address", "HQ")
    person("AB0018", "Casual", travel_eligible=True)
    with SessionLocal() as db:
        db.add(Venue(name="far client", label="Far Client", kind="client", address="Far St"))
        db.add(TravelLeg(origin="HQ", destination="Far St", km=90, minutes=95, source="manual"))
        db.add(TravelLeg(origin="Far St", destination="HQ", km=90, minutes=95, source="manual"))
        db.commit()
    mon, tue = date(2026, 10, 5), date(2026, 10, 6)
    roster(source, (mon, "AB0018", "09:00", "12:00", "First Aid", "Tullamarine", 2.5),
           (tue, "AB0018", "08:00", "16:00", "First Aid", "Far Client", 6.0))
    clock("AB0018", mon, "09:20", "11:55")   # 20 late + 5 early = 25 min → 0.5 h docked
    clock("AB0018", tue, "08:00", "16:00")
    _, by = build()
    p = by["AB0018"]
    it = items(p)
    # Mon: 0.5 buffer − 0.5 docked = 0. Tue: 0.5 buffer + 190 min travel (3.17 h) = 3.67.
    assert it["Non-Teaching Duties"] == 3.67 and it["Sessional Teaching"] == 2.5 + 5
    text = " | ".join(p["flags"])
    assert "20 min late and left 5 min early — 0.5 h docked from Non-Teaching Duties" in text
    assert "8.67 h paid (teaching 5 + non-teaching 3.67) — over 7.6 h in a day" in text


def test_admin_sets_staff_email(client, source):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None)
    add_staff("AB0011", "taken@x.au")
    login(client, "AB0001")
    assert client.post("/api/admin/staff/AB0010/email", json={"email": "Sonya@X.au"}).json()["email"] == "sonya@x.au"
    assert client.post("/api/admin/staff/AB0010/email", json={"email": "taken@x.au"}).status_code == 409
    assert client.post("/api/admin/staff/AB0010/email", json={"email": "nope"}).status_code == 400
    assert client.post("/api/admin/staff/AB0010/email", json={"email": ""}).json()["email"] is None
