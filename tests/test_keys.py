"""Key register: issue to Australian staff, accept with a typed signature, return / lost, leavers flagged."""

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import EmailOutbox, KeyIssue, Staff
from tests.conftest import add_staff, login


@pytest.fixture
def setup(client, source, monkeypatch):
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p"), ("alert_email", "brendan@x.au")):
        monkeypatch.setattr(s, k, v)
    add_staff("AB0001", "brendan@x.au", role="admin")
    add_staff("AB0010", "sonya@x.au")
    add_staff("AB0003", None)
    with SessionLocal() as db:
        db.get(Staff, "AB0010").name = "Sonya Wilkinson"
        db.get(Staff, "AB0003").location_type = "offshore"
        db.commit()
    login(client, "AB0001")
    for kind, n in (("key", "12"), ("remote", "3")):
        assert client.post("/api/admin/keys", json={"kind": kind, "number": n}).status_code == 200
    assert client.post("/api/admin/keys", json={"kind": "key", "number": "12"}).status_code == 409  # duplicate
    return client


def ids(c):
    return [i["id"] for i in c.get("/api/admin/keys").json()["items"]]


def test_issue_accept_return(setup):
    c = setup
    items = ids(c)
    assert c.post("/api/admin/keys/issue", json={"staff_id": "AB0003", "item_ids": items}).status_code == 400  # offshore
    assert c.post("/api/admin/keys/issue", json={"staff_id": "AB0010", "item_ids": items}).status_code == 200
    assert c.post("/api/admin/keys/issue", json={"staff_id": "AB0001", "item_ids": items[:1]}).status_code == 409  # already out
    with SessionLocal() as db:
        assert db.query(EmailOutbox).filter_by(to_addr="sonya@x.au").count() == 1
    c.post("/api/auth/logout")
    login(c, "AB0010")
    me = c.get("/api/keys/me").json()
    assert len(me["pending"]) == 2 and "property of AB Group" in me["terms"]
    pend = [i["id"] for i in me["pending"]]
    assert c.post("/api/keys/me/accept", json={"issue_ids": pend, "signed_name": "Sonya", "agree": True}).status_code == 400
    assert c.post("/api/keys/me/accept", json={"issue_ids": pend, "signed_name": "Sonya Wilkinson", "agree": False}).status_code == 400
    assert c.post("/api/keys/me/accept", json={"issue_ids": pend, "signed_name": " sonya  wilkinson ", "agree": True}).status_code == 200
    me = c.get("/api/keys/me").json()
    assert len(me["held"]) == 2 and me["pending"] == []
    with SessionLocal() as db:
        i = db.query(KeyIssue).first()
        assert i.signed_name == "sonya  wilkinson" and i.accepted_at and "property of AB Group" in i.terms
    assert c.get("/api/admin/keys").status_code == 403
    c.post("/api/auth/logout")
    login(c, "AB0001")
    reg = c.get("/api/admin/keys").json()
    key = next(i for i in reg["items"] if i["label"] == "Key 12")
    assert key["current"]["status"] == "held"
    assert c.post(f"/api/admin/keys/issues/{key['current']['id']}/close", json={"outcome": "cancelled"}).status_code == 409
    assert c.post(f"/api/admin/keys/issues/{key['current']['id']}/close", json={"outcome": "returned"}).status_code == 200
    remote = next(i for i in c.get("/api/admin/keys").json()["items"] if i["label"] == "Remote 3")
    c.post(f"/api/admin/keys/issues/{remote['current']['id']}/close", json={"outcome": "lost", "note": "Left on train"})
    reg = {i["label"]: i for i in c.get("/api/admin/keys").json()["items"]}
    assert reg["Key 12"]["current"] is None and reg["Key 12"]["history"][0]["status"] == "returned"
    assert reg["Remote 3"]["retired"] is True


def test_leaver_with_keys_is_flagged(setup):
    c = setup
    c.post("/api/admin/keys/issue", json={"staff_id": "AB0010", "item_ids": ids(c)[:1]})
    r = c.post("/api/admin/staff/AB0010/active", json={"active": False})
    assert r.status_code == 200 and r.json()["keys"] == ["Key 12"]
    with SessionLocal() as db:
        assert db.query(EmailOutbox).filter(EmailOutbox.subject.contains("has left with keys")).count() == 1
    reg = c.get("/api/admin/keys").json()
    assert reg["leavers"] == [{"staff_id": "AB0010", "name": "Sonya Wilkinson", "items": ["Key 12"]}]
    assert c.post("/api/admin/staff/AB0001/active", json={"active": False}).status_code == 400  # not yourself
