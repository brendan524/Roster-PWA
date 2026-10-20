from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api import leave as leave_api
from app.db import SessionLocal
from app.leave import OFFSHORE_TYPES, accrual_dates, add_months, compute_balance, count_days
from app.models import Staff
from tests.conftest import add_staff, login

VAC, SICK = OFFSHORE_TYPES[0], OFFSHORE_TYPES[1]


def req(start, end, days, status="approved", code="vacation"):
    return SimpleNamespace(leave_type=code, start_date=start, end_date=end, days=Decimal(str(days)), status=status)


def adj(kind, amount, on, code="vacation", reason="test"):
    return SimpleNamespace(leave_type=code, kind=kind, amount=Decimal(str(amount)), effective_date=on, reason=reason)


# ---- policy maths -------------------------------------------------------------------

def test_first_credit_at_start_of_month_7_then_monthly():
    join = date(2026, 3, 15)
    assert accrual_dates(join, date(2026, 9, 14)) == []
    assert accrual_dates(join, date(2026, 11, 15)) == [date(2026, 9, 15), date(2026, 10, 15), date(2026, 11, 15)]


def test_month_end_join_dates_clamp():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)  # leap year
    assert accrual_dates(date(2026, 3, 31), date(2026, 12, 31)) == [
        date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31)]


def test_vacation_and_sick_rates():
    join, today = date(2025, 1, 10), date(2026, 1, 10)  # 7 credits: Jul..Jan
    assert compute_balance(VAC, join, today, [], []).balance == 7
    assert compute_balance(SICK, join, today, [], []).balance == Decimal("3.5")


def test_vacation_caps_at_10_sick_does_not():
    join, today = date(2023, 1, 1), date(2026, 1, 1)  # 31 credits
    vac = compute_balance(VAC, join, today, [], [])
    assert vac.balance == 10
    assert vac.history[-1].note.startswith("at the 10-day limit")
    assert compute_balance(SICK, join, today, [], []).balance == Decimal("15.5")


def test_cap_partial_credit_after_taking_leave():
    join = date(2024, 1, 1)
    # At the cap by mid-2025; takes 0.5 day on 2 Jun; next credit (1 Jul) only refills 0.5.
    b = compute_balance(VAC, join, date(2025, 7, 1), [req(date(2025, 6, 2), date(2025, 6, 2), 0.5)], [])
    assert b.balance == 10
    assert b.history[-1].amount == Decimal("0.5") and "capped" in b.history[-1].note


def test_future_approved_leave_is_booked_not_yet_deducted():
    join, today = date(2025, 1, 1), date(2026, 1, 20)
    b = compute_balance(VAC, join, today, [req(date(2026, 2, 2), date(2026, 2, 4), 3)], [])
    assert (b.balance, b.booked, b.available) == (7, 3, 4)  # 7 credits: Jul 2025..Jan 2026


def test_pending_and_declined_dont_change_the_balance():
    join, today = date(2025, 1, 1), date(2026, 1, 20)
    b = compute_balance(VAC, join, today, [req(date(2026, 1, 5), date(2026, 1, 5), 1, "pending"),
                                           req(date(2026, 1, 6), date(2026, 1, 6), 1, "declined")], [])
    assert (b.balance, b.pending) == (7, 1)


def test_opening_balance_replaces_earlier_history():
    join, today = date(2024, 1, 1), date(2026, 1, 20)
    b = compute_balance(VAC, join, today, [req(date(2025, 3, 3), date(2025, 3, 7), 5)],
                        [adj("set", 4.5, date(2026, 1, 1))])
    assert b.balance == Decimal("4.5")  # a set balance is final for its date (same-day credit not added on top)
    b2 = compute_balance(VAC, join, date(2026, 2, 1), [], [adj("set", 4.5, date(2026, 1, 1))])
    assert b2.balance == Decimal("5.5")


def test_add_adjustment_and_next_credit_during_probation():
    b = compute_balance(SICK, date(2026, 6, 1), date(2026, 7, 1), [], [adj("add", 2, date(2026, 6, 15), "sick")])
    assert b.balance == 2 and b.next_date == date(2026, 12, 1) and b.next_amount == Decimal("0.5")


def test_count_days():
    mon, fri, sun = date(2026, 10, 19), date(2026, 10, 23), date(2026, 10, 25)
    assert count_days(mon, fri) == 5
    assert count_days(mon, sun) == 5  # weekend not counted
    assert count_days(mon, fri, start_half=True, end_half=True) == 4
    assert count_days(mon, mon, start_half=True) == Decimal("0.5")
    assert count_days(mon, mon, start_half=True, end_half=True) == Decimal("0.5")  # one day can't be two halves
    assert count_days(sun, sun) == 0


# ---- API ------------------------------------------------------------------------------

@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(leave_api, "today", lambda: date(2026, 10, 15))
    return date(2026, 10, 15)


def offshore(staff_id, join=date(2026, 3, 15)):
    add_staff(staff_id, None)
    with SessionLocal() as db:
        s = db.get(Staff, staff_id)
        s.location_type, s.join_date = "offshore", join
        db.commit()


def apply(client, start, end, code="vacation", **kw):
    body = {"leave_type": code, "start_date": start, "end_date": end, "reason": "Family", **kw}
    return client.post("/api/leave/requests", json=body)


def test_offshore_balance_view(client, fixed_today):
    offshore("AB0003")
    login(client, "AB0003")
    me = client.get("/api/leave/me").json()
    vac = next(b for b in me["balances"] if b["code"] == "vacation")
    assert (vac["balance"], vac["next_date"]) == (2, "2026-11-15")
    assert [t["code"] for t in me["types"]] == ["vacation", "sick", "unpaid"]


def test_onshore_has_no_app_balance_and_own_types(client, fixed_today):
    add_staff("AB0010", None)
    login(client, "AB0010")
    me = client.get("/api/leave/me").json()
    assert me["balances"] is None and "annual" in [t["code"] for t in me["types"]]
    assert apply(client, "2026-10-27", "2026-10-27", code="vacation").status_code == 400
    assert apply(client, "2026-10-27", "2026-10-27", code="annual").status_code == 200


def test_apply_approve_and_balance_updates(client, fixed_today):
    offshore("AB0003")
    add_staff("AB0001", None, role="admin")
    login(client, "AB0003")
    r = apply(client, "2026-10-26", "2026-10-30", end_half=True, reason="Family trip")  # Mon–Fri, half Fri
    assert r.status_code == 200 and r.json()["days"] == 4.5 and r.json()["status"] == "pending"
    rid = r.json()["id"]
    client.post("/api/auth/logout")
    login(client, "AB0001")
    admin = client.get("/api/admin/leave").json()
    [p] = admin["pending"]
    assert p["balance"] == {"available": 2.0, "after": -2.5, "short_by": 2.5}  # flagged, not blocked
    assert client.post(f"/api/admin/leave/{rid}/decide", json={"decision": "approve", "note": "ok"}).status_code == 200
    assert client.post(f"/api/admin/leave/{rid}/decide", json={"decision": "decline"}).status_code == 409
    client.post("/api/auth/logout")
    login(client, "AB0003")
    vac = next(b for b in client.get("/api/leave/me").json()["balances"] if b["code"] == "vacation")
    assert (vac["balance"], vac["booked"], vac["available"]) == (2, 4.5, -2.5)


def test_overlapping_requests_are_refused(client, fixed_today):
    offshore("AB0003")
    login(client, "AB0003")
    assert apply(client, "2026-10-26", "2026-10-28").status_code == 200
    r = apply(client, "2026-10-28", "2026-10-29")
    assert r.status_code == 409 and "already" in r.json()["detail"]


def test_cancel_only_own_pending(client, fixed_today):
    offshore("AB0003")
    offshore("AB0006")
    login(client, "AB0003")
    rid = apply(client, "2026-10-26", "2026-10-26").json()["id"]
    client.post("/api/auth/logout")
    login(client, "AB0006")
    assert client.post(f"/api/leave/requests/{rid}/cancel").status_code == 404  # not theirs
    client.post("/api/auth/logout")
    login(client, "AB0003")
    assert client.post(f"/api/leave/requests/{rid}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/leave/requests/{rid}/cancel").status_code == 409
    assert apply(client, "2026-10-26", "2026-10-26").status_code == 200  # dates free again


@pytest.mark.parametrize("start,end,msg", [
    ("2026-10-30", "2026-10-26", "before the start"),
    ("2026-10-24", "2026-10-25", "working days"),
    ("2026-10-15", "2026-10-16", "notice"),  # vacation can't start today
    ("2026-10-01", "2026-10-02", "notice"),
    ("2026-11-01", "2027-03-01", "at most"),
])
def test_bad_requests(client, fixed_today, start, end, msg):
    offshore("AB0003")
    login(client, "AB0003")
    r = apply(client, start, end)
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_staff_cannot_approve(client, fixed_today):
    offshore("AB0003")
    login(client, "AB0003")
    rid = apply(client, "2026-10-26", "2026-10-26").json()["id"]
    assert client.get("/api/admin/leave").status_code == 403
    assert client.post(f"/api/admin/leave/{rid}/decide", json={"decision": "approve"}).status_code == 403


def test_staff_only_see_their_own_requests(client, fixed_today):
    offshore("AB0003")
    offshore("AB0006")
    login(client, "AB0003")
    apply(client, "2026-10-26", "2026-10-26", reason="private reason")
    client.post("/api/auth/logout")
    login(client, "AB0006")
    body = client.get("/api/leave/me").json()
    assert body["requests"] == [] and "private reason" not in str(body)


def test_join_date_missing(client, fixed_today):
    offshore("AB0003", join=None)
    login(client, "AB0003")
    assert client.get("/api/leave/me").json()["balances"] is None
    assert apply(client, "2026-10-26", "2026-10-26").status_code == 200  # can still apply


def test_far_future_dates_still_need_working_days(client, fixed_today):
    offshore("AB0003")
    login(client, "AB0003")
    sat = (date(2026, 10, 15) + timedelta(days=(5 - date(2026, 10, 15).weekday()) % 7)).isoformat()
    assert apply(client, sat, sat).status_code == 400


def test_reason_is_required(client, fixed_today):
    offshore("AB0003")
    login(client, "AB0003")
    for reason in ("", "  ", "x"):
        r = apply(client, "2026-10-26", "2026-10-26", reason=reason)
        assert r.status_code == 400 and "reason" in r.json()["detail"]


@pytest.mark.parametrize("loc,code", [("offshore", "sick"), ("onshore", "personal")])
def test_sick_and_personal_can_be_backdated(client, fixed_today, loc, code):
    if loc == "offshore":
        offshore("AB0003")
    else:
        add_staff("AB0003", None)
    login(client, "AB0003")
    assert apply(client, "2026-10-12", "2026-10-13", code=code, reason="Flu").status_code == 200
    assert apply(client, "2026-10-15", "2026-10-15", code=code, reason="Flu again").status_code == 200  # today
    r = apply(client, "2026-08-10", "2026-08-10", code=code, reason="Old")
    assert r.status_code == 400 and "60 days" in r.json()["detail"]


@pytest.mark.parametrize("code", ["annual", "long_service", "unpaid"])
def test_onshore_advance_only_types(client, fixed_today, code):
    add_staff("AB0010", None)
    with SessionLocal() as db:
        db.get(Staff, "AB0010").join_date = date(2015, 1, 1)  # long service needs 7 years
        db.commit()
    login(client, "AB0010")
    r = apply(client, "2026-10-21", "2026-10-21", code=code)  # 6 days' notice
    assert r.status_code == 400 and "earliest start date is Thu 22 Oct" in r.json()["detail"]
    assert apply(client, "2026-10-22", "2026-10-22", code=code).status_code == 200


# ---- attachments ----------------------------------------------------------------------

PDF = b"%PDF-1.7\n%fake but has the right header\n"
JPG = b"\xff\xd8\xff\xe0" + b"0" * 100
HEIC = b"\x00\x00\x00\x18ftypheic" + b"0" * 50


def upload(client, rid, data, name="cert.pdf"):
    return client.post(f"/api/leave/requests/{rid}/attachments", content=data,
                       headers={"x-filename": name, "content-type": "application/octet-stream"})


def test_attach_view_and_permissions(client, fixed_today):
    offshore("AB0003")
    offshore("AB0006")
    add_staff("AB0001", None, role="admin")
    login(client, "AB0003")
    rid = apply(client, "2026-10-12", "2026-10-12", code="sick", reason="Flu").json()["id"]
    r = upload(client, rid, PDF, "Medical cert (Dr Smith).pdf")
    assert r.status_code == 200 and r.json()["content_type"] == "application/pdf"
    aid = r.json()["id"]
    assert r.json()["filename"] == "Medical cert (Dr Smith).pdf"
    assert upload(client, rid, JPG, "photo.jpeg").json()["filename"] == "photo.jpg"
    assert upload(client, rid, HEIC, "IMG_1234.HEIC").json()["content_type"] == "image/heic"
    me = client.get("/api/leave/me").json()
    assert [a["filename"] for a in me["requests"][0]["attachments"]] == [
        "Medical cert (Dr Smith).pdf", "photo.jpg", "IMG_1234.heic"]
    got = client.get(f"/api/leave/attachments/{aid}")
    assert got.status_code == 200 and got.content == PDF and got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]
    client.post("/api/auth/logout")
    login(client, "AB0006")  # another staff member: can't see it, can't add to it
    assert client.get(f"/api/leave/attachments/{aid}").status_code == 404
    assert upload(client, rid, PDF).status_code == 404
    client.post("/api/auth/logout")
    login(client, "AB0001")  # admin: can see it, and it's on the pending card
    assert client.get(f"/api/leave/attachments/{aid}").status_code == 200
    [p] = client.get("/api/admin/leave").json()["pending"]
    assert len(p["attachments"]) == 3


def test_attachment_rules(client, fixed_today):
    offshore("AB0003")
    login(client, "AB0003")
    rid = apply(client, "2026-10-26", "2026-10-26", reason="Trip").json()["id"]
    assert upload(client, rid, b"MZ\x90\x00 not a pdf", "virus.pdf").status_code == 415  # real type checked
    assert upload(client, rid, b"<html><script>alert(1)</script>", "x.png").status_code == 415
    assert upload(client, rid, b"").status_code == 400
    assert upload(client, rid, b"%PDF-" + b"0" * (10 * 1024 * 1024)).status_code == 413
    for _ in range(5):
        assert upload(client, rid, PDF).status_code == 200
    assert upload(client, rid, PDF).status_code == 409  # max 5
    # can remove while pending; not after approval/cancel
    aid = client.get("/api/leave/me").json()["requests"][0]["attachments"][0]["id"]
    assert client.delete(f"/api/leave/attachments/{aid}").status_code == 200
    client.post(f"/api/leave/requests/{rid}/cancel")
    assert upload(client, rid, PDF).status_code == 409


@pytest.mark.parametrize("loc,status,expected", [
    ("onshore", "Contract", False), ("onshore", "contractor", False), ("offshore", "Contractor", True),
    ("onshore", "Casual", True), ("onshore", "Part-time", True), ("onshore", "", True),
])
def test_default_has_leave(loc, status, expected):
    from app.leave import default_has_leave
    assert default_has_leave(loc, status) is expected


def test_long_service_ok():
    from app.leave import long_service_ok
    assert not long_service_ok(None, date(2030, 1, 1))
    assert not long_service_ok(date(2019, 10, 16), date(2026, 10, 15))
    assert long_service_ok(date(2019, 10, 15), date(2026, 10, 15))


def test_short_notice_request_needs_acknowledgement(client, fixed_today, monkeypatch):
    from app.config import get_settings
    from app.models import EmailOutbox
    s = get_settings()
    for k, v in (("smtp_user", "hr@x.au"), ("smtp_password", "p")):
        monkeypatch.setattr(s, k, v)
    add_staff("AB0001", "brendan@x.au", role="admin")
    add_staff("AB0010", None)
    login(client, "AB0010")
    assert apply(client, "2026-10-19", "2026-10-19", code="annual").status_code == 400  # 4 days, not acknowledged
    assert apply(client, "2026-10-14", "2026-10-14", code="annual", short_notice=True).status_code == 400  # past
    r = apply(client, "2026-10-19", "2026-10-20", code="annual", short_notice=True)
    assert r.status_code == 200 and r.json()["short_notice"] is True
    later = apply(client, "2026-11-02", "2026-11-02", code="annual", short_notice=True)  # ticked but not short
    assert later.json()["short_notice"] is False
    with SessionLocal() as db:
        subjects = [m.subject for m in db.query(EmailOutbox).order_by(EmailOutbox.id)]
    assert subjects[0].startswith("SHORT NOTICE Leave request: Ab0010") and not subjects[1].startswith("SHORT")
