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
    return client.post("/api/leave/requests", json={"leave_type": code, "start_date": start, "end_date": end, **kw})


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
    assert apply(client, "2026-10-20", "2026-10-20", code="vacation").status_code == 400
    assert apply(client, "2026-10-20", "2026-10-20", code="annual").status_code == 200


def test_apply_approve_and_balance_updates(client, fixed_today):
    offshore("AB0003")
    add_staff("AB0001", None, role="admin")
    login(client, "AB0003")
    r = apply(client, "2026-10-19", "2026-10-23", end_half=True, reason="Family trip")  # Mon–Fri, half Fri
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
    assert apply(client, "2026-10-19", "2026-10-21").status_code == 200
    r = apply(client, "2026-10-21", "2026-10-22")
    assert r.status_code == 409 and "already" in r.json()["detail"]


def test_cancel_only_own_pending(client, fixed_today):
    offshore("AB0003")
    offshore("AB0006")
    login(client, "AB0003")
    rid = apply(client, "2026-10-19", "2026-10-19").json()["id"]
    client.post("/api/auth/logout")
    login(client, "AB0006")
    assert client.post(f"/api/leave/requests/{rid}/cancel").status_code == 404  # not theirs
    client.post("/api/auth/logout")
    login(client, "AB0003")
    assert client.post(f"/api/leave/requests/{rid}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/leave/requests/{rid}/cancel").status_code == 409
    assert apply(client, "2026-10-19", "2026-10-19").status_code == 200  # dates free again


@pytest.mark.parametrize("start,end,msg", [
    ("2026-10-23", "2026-10-19", "before the start"),
    ("2026-10-24", "2026-10-25", "working days"),
    ("2026-01-01", "2026-01-02", "60 days ago"),
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
    rid = apply(client, "2026-10-19", "2026-10-19").json()["id"]
    assert client.get("/api/admin/leave").status_code == 403
    assert client.post(f"/api/admin/leave/{rid}/decide", json={"decision": "approve"}).status_code == 403


def test_staff_only_see_their_own_requests(client, fixed_today):
    offshore("AB0003")
    offshore("AB0006")
    login(client, "AB0003")
    apply(client, "2026-10-19", "2026-10-19", reason="private reason")
    client.post("/api/auth/logout")
    login(client, "AB0006")
    body = client.get("/api/leave/me").json()
    assert body["requests"] == [] and "private reason" not in str(body)


def test_join_date_missing(client, fixed_today):
    offshore("AB0003", join=None)
    login(client, "AB0003")
    assert client.get("/api/leave/me").json()["balances"] is None
    assert apply(client, "2026-10-19", "2026-10-19").status_code == 200  # can still apply


def test_far_future_dates_still_need_working_days(client, fixed_today):
    offshore("AB0003")
    login(client, "AB0003")
    sat = (date(2026, 10, 15) + timedelta(days=(5 - date(2026, 10, 15).weekday()) % 7)).isoformat()
    assert apply(client, sat, sat).status_code == 400
