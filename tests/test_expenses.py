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


def claim(client, **kw):
    body = {"expense_date": "2026-10-12", "category": "parking", "amount": "18.50", "description": "Parking at Acme"} | kw
    return client.post("/api/expenses", json=body)


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
    service = app.dependency_overrides[get_roster_service]()
    now = datetime(2026, 10, 19, 6, tzinfo=UTC)
    with SessionLocal() as db:
        rep = payreport.build(db, service, date(2026, 10, 18), now=now)
        by = {p["staff_id"]: p for p in rep["people"]}
        assert {i["item"]: i["qty"] for i in by["AB0016"]["items"]} == {"Reimbursement": 18.5}
        assert {i["item"]: i["qty"] for i in by["AB0003"]["items"]} == {"Reimbursement (PHP)": 1500.0, "Days worked": 0}
        assert "Expense: Mon 12 Oct: $18.50 — Parking at Acme" in payreport.email_text(rep)
        send(db, rep, now)
        db.commit()
        assert db.get(ExpenseClaim, cid).paid_in_period == date(2026, 10, 18)
        nxt = payreport.build(db, service, date(2026, 11, 1), now=now + timedelta(days=14))
    assert "AB0016" not in {p["staff_id"] for p in nxt["people"]}  # not paid twice
