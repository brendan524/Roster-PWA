"""Expense claims: submit with receipt, only the approver decides, paid through the pay report once."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app import payreport
from app.api import leave as leave_api
from app.api.pay import send
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_roster_service
from app.main import app
from app.models import EmailOutbox, ExpenseClaim, Staff
from tests.conftest import HEADER, add_staff, login

JPG = b"\xff\xd8\xff\xe0" + b"0" * 100


@pytest.fixture
def setup(client, source, monkeypatch):
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p"), ("alert_email", "brendan@x.au"),
                 ("expense_approvers", "AB0001")):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(leave_api, "today", lambda: date(2026, 10, 15))
    import app.api.expenses as ex
    monkeypatch.setattr(ex, "today", lambda: date(2026, 10, 15))
    add_staff("AB0001", "brendan@x.au", role="admin")
    add_staff("AB0002", "aaron@x.au", role="admin")
    add_staff("AB0016", "jody@x.au")
    add_staff("AB0003", None)
    with SessionLocal() as db:
        db.get(Staff, "AB0003").location_type = "offshore"
        db.commit()
    source.tabs = {"2026-10": [HEADER], "2026-11": [HEADER]}
    return client


def backdate_approvals():
    """Tests build the 5–18 Oct report; claims approved "today" in real time would belong to a later one."""
    with SessionLocal() as db:
        for x in db.query(ExpenseClaim).filter(ExpenseClaim.decided_at.isnot(None)):
            x.decided_at = datetime(2026, 10, 17, tzinfo=UTC)
        db.commit()


def claim(http, **kw):
    body = {"expense_date": "2026-10-12", "category": "parking", "amount": "18.50", "description": "Parking at Acme"} | kw
    return http.post("/api/expenses", json=body)


def upload(client, cid, data=JPG):
    return client.post(f"/api/expenses/{cid}/attachments", content=data, headers={"x-filename": "receipt.jpg"})


def test_submit_validate_and_notify(setup):
    c = setup
    login(c, "AB0016")
    assert claim(c, category="nope").status_code == 400
    assert claim(c, description="").status_code == 400
    assert claim(c, amount="0").status_code == 422
    assert claim(c, expense_date="2026-10-16").status_code == 400  # future
    assert claim(c, expense_date="2026-06-01").status_code == 400  # over 90 days
    r = claim(c)
    assert r.status_code == 200 and r.json()["amount_text"] == "$18.50" and r.json()["currency"] == "AUD"
    assert upload(c, r.json()["id"], b"not an image").status_code == 415
    assert upload(c, r.json()["id"]).status_code == 200
    with SessionLocal() as db:
        [m] = db.query(EmailOutbox).all()
    assert m.to_addr == "brendan@x.au" and m.subject == "Expense claim: Ab0016 — $18.50"  # only the approver
    me = c.get("/api/expenses/me").json()
    assert len(me["claims"]) == 1 and me["claims"][0]["attachments"][0]["filename"] == "receipt.jpg"


def test_only_approver_decides_and_needs_receipt(setup):
    c = setup
    login(c, "AB0016")
    cid = claim(c).json()["id"]
    c.post("/api/auth/logout")
    login(c, "AB0002")  # Aaron: admin, can see but not approve
    a = c.get("/api/admin/expenses").json()
    assert a["can_approve"] is False and len(a["pending"]) == 1
    assert c.post(f"/api/admin/expenses/{cid}/decide", json={"decision": "approve"}).status_code == 403
    c.post("/api/auth/logout")
    login(c, "AB0001")
    assert c.post(f"/api/admin/expenses/{cid}/decide", json={"decision": "approve"}).status_code == 409  # no receipt
    assert c.post(f"/api/admin/expenses/{cid}/decide", json={"decision": "decline"}).status_code == 400  # needs reason
    c.post("/api/auth/logout")
    login(c, "AB0016")
    upload(c, cid)
    c.post("/api/auth/logout")
    login(c, "AB0001")
    r = c.post(f"/api/admin/expenses/{cid}/decide", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    with SessionLocal() as db:
        assert db.query(EmailOutbox).filter_by(to_addr="jody@x.au", subject="Your expense claim was approved").count() == 1
    # Receipts: owner and admins only.
    att = r.json()["attachments"][0]["id"]
    assert c.get(f"/api/expenses/attachments/{att}").status_code == 200
    c.post("/api/auth/logout")
    login(c, "AB0003")
    assert c.get(f"/api/expenses/attachments/{att}").status_code == 404


def test_cannot_approve_own_claim(setup):
    c = setup
    login(c, "AB0001")
    cid = claim(c).json()["id"]
    upload(c, cid)
    assert c.post(f"/api/admin/expenses/{cid}/decide", json={"decision": "approve"}).status_code == 403


def test_offshore_in_pesos_and_paid_once_in_pay_report(setup):
    c = setup
    login(c, "AB0003")
    r = claim(c, amount="1500", description="Internet top-up")
    assert r.json()["currency"] == "PHP" and r.json()["amount_text"] == "₱1,500.00"
    upload(c, r.json()["id"])
    c.post("/api/auth/logout")
    login(c, "AB0016")
    cid = claim(c).json()["id"]
    upload(c, cid)
    c.post("/api/auth/logout")
    login(c, "AB0001")
    for x in c.get("/api/admin/expenses").json()["pending"]:
        c.post(f"/api/admin/expenses/{x['id']}/decide", json={"decision": "approve"})
    backdate_approvals()
    service = app.dependency_overrides[get_roster_service]()
    now = datetime(2026, 10, 19, 6, tzinfo=UTC)
    with SessionLocal() as db:
        rep = payreport.build(db, service, date(2026, 10, 18), now=now)
        by = {p["staff_id"]: p for p in rep["people"]}
        assert {i["item"]: i["qty"] for i in by["AB0016"]["items"]} == {"Reimbursement": 18.5}
        assert {i["item"]: i["qty"] for i in by["AB0003"]["items"]} == {"Reimbursement (PHP)": 1500.0, "Days worked": 0}
        assert "Claim: Mon 12 Oct: $18.50 — Parking at Acme" in payreport.email_text(rep)
        send(db, rep, now)
        db.commit()
        assert db.get(ExpenseClaim, cid).paid_in_period == date(2026, 10, 18)
        nxt = payreport.build(db, service, date(2026, 11, 1), now=now + timedelta(days=14))
    assert "AB0016" not in {p["staff_id"] for p in nxt["people"]}  # not paid twice


def test_offshore_incentive_claims(setup):
    c = setup
    login(c, "AB0016")  # onshore: no incentives
    assert claim(c, category="incentive", client="Acme", description="Retained").status_code == 403
    assert c.get("/api/expenses/me").json()["incentives"] is False
    c.post("/api/auth/logout")
    login(c, "AB0003")
    assert c.get("/api/expenses/me").json()["incentives"] is True
    assert claim(c, category="incentive", client="Acme", description="Retained client").status_code == 400  # no type
    assert claim(c, category="incentive", incentive_type="retention", client="", description="Retained client").status_code == 400
    r = claim(c, category="incentive", incentive_type="retention", client="Acme Gym", amount="2000",
              description="Client renewed for 12 months")
    assert r.status_code == 200 and r.json()["kind"] == "incentive" and r.json()["amount_text"] == "₱2,000.00"
    with SessionLocal() as db:
        m = db.query(EmailOutbox).filter(EmailOutbox.subject.startswith("Incentive claim")).one()
        assert "client: Acme Gym" in m.body
    c.post("/api/auth/logout")
    login(c, "AB0001")
    d = c.post(f"/api/admin/expenses/{r.json()['id']}/decide", json={"decision": "approve"})  # no receipt needed
    assert d.status_code == 200
    backdate_approvals()
    service = app.dependency_overrides[get_roster_service]()
    with SessionLocal() as db:
        rep = payreport.build(db, service, date(2026, 10, 18), now=datetime(2026, 10, 19, 6, tzinfo=UTC))
    p = next(p for p in rep["people"] if p["staff_id"] == "AB0003")
    assert {i["item"]: i["qty"] for i in p["items"]}["Incentive (PHP)"] == 2000.0
    assert "Incentive, Acme Gym: Client renewed for 12 months" in payreport.email_text(rep)



def test_monthly_incentive_with_booking_table(setup):
    c = setup
    login(c, "AB0003")
    rows = [
        {"Booking Date": "01/09/2026", "Time": "5.00PM", "Organization": "Resolution Drilling", "Course": "FA, CPR",
         "Participants": "21", "Trainer": "Aaron", "Invoice Amount": "$2650", "Blue": "https://blue.example/1",
         "TD Portal": "javascript:alert(1)", "Booking Spreadsheet": "Resolution Drilling", "Invoice Status": "Paid"},
        {"Booking Date": "08/09/2026", "Organization": "The Photo Studio", "Trainer": "Craig", "Invoice Status": "Paid"},
        {},
    ]
    assert claim(c, category="incentive", incentive_type="monthly", amount="3000", expense_date="2026-09-01",
                 description="", rows=[]).status_code == 400  # needs bookings
    r = claim(c, category="incentive", incentive_type="monthly", amount="3000", expense_date="2026-09-01",
              description="", rows=rows)
    assert r.status_code == 200
    got = r.json()
    assert got["incentive_label"] == "Monthly incentive" and len(got["rows"]) == 2  # empty row dropped
    assert got["rows"][0]["TD Portal"] == "" and got["rows"][0]["Blue"] == "https://blue.example/1"
    with SessionLocal() as db:
        m = db.query(EmailOutbox).filter(EmailOutbox.subject.startswith("Incentive claim")).one()
    assert "Monthly incentive, September 2026 — 2 booking(s)" in m.body
    assert "Resolution Drilling · FA, CPR · 21 participants · Aaron · $2650 · Paid" in m.body
    assert "Blue: https://blue.example/1" in m.body and "javascript" not in m.body
    c.post("/api/auth/logout")
    login(c, "AB0001")
    c.post(f"/api/admin/expenses/{got['id']}/decide", json={"decision": "approve"})
    backdate_approvals()
    service = app.dependency_overrides[get_roster_service]()
    with SessionLocal() as db:
        rep = payreport.build(db, service, date(2026, 10, 18), now=datetime(2026, 10, 19, 6, tzinfo=UTC))
    assert "Monthly incentive, September 2026 (2 bookings)" in payreport.email_text(rep)
