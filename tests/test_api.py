from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import app
from app.roster import month_tab, roster_months
from tests.conftest import HEADER, add_staff, login


def today():
    return datetime.now(ZoneInfo("Australia/Melbourne")).date()


def tabs_with(*rows):
    cur, nxt = roster_months(today())
    d = f"{cur}-15"
    return {cur: [HEADER] + [[d, sid, sid, "Gym", "Class", "06:00", "07:00", "Gym", "", "", ""] for sid in rows],
            nxt: [HEADER]}


def test_login_and_me(client):
    add_staff("AB0003", "sonya@example.com")
    r = login(client, "  Sonya@Example.com ")
    assert r.status_code == 200 and r.json()["staff_id"] == "AB0003"
    assert client.get("/api/auth/me").json()["name"] == "Ab0003"


def test_login_with_staff_id_any_case(client):
    add_staff("AB0010", None)
    r = login(client, " ab0010 ")
    assert r.status_code == 200 and r.json()["staff_id"] == "AB0010"


def test_wrong_password_and_unknown_email_look_the_same(client):
    add_staff("AB0003", "sonya@example.com")
    a = login(client, "sonya@example.com", "wrong-password")
    b = login(client, "nobody@example.com", "wrong-password")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_lockout_after_repeated_failures(client):
    add_staff("AB0003", "sonya@example.com")
    for _ in range(5):
        assert login(client, "sonya@example.com", "nope-nope-nope").status_code == 401
    # Locked, even with the right password.
    assert login(client, "sonya@example.com").status_code == 429


def test_inactive_staff_cannot_log_in(client):
    add_staff("AB0003", "sonya@example.com", active=False)
    assert login(client, "sonya@example.com").status_code == 401


def test_requires_login(client, source):
    assert client.get("/api/roster/me").status_code == 401
    assert client.get("/api/admin/roster").status_code == 401


def test_csrf_header_required_for_writes(source):
    add_staff("AB0003", "sonya@example.com")
    with TestClient(app) as bare:
        r = bare.post("/api/auth/login", json={"login": "sonya@example.com", "password": "correct-horse-1"})
        assert r.status_code == 403


def test_must_change_password_blocks_roster_until_changed(client, source):
    add_staff("AB0003", "sonya@example.com", must_change=True)
    source.tabs = tabs_with("AB0003")
    assert login(client, "sonya@example.com").json()["must_change_password"] is True
    assert client.get("/api/roster/me").status_code == 403

    r = client.post("/api/auth/change-password",
                    json={"current_password": "correct-horse-1", "new_password": "short"})
    assert r.status_code == 400
    r = client.post("/api/auth/change-password",
                    json={"current_password": "correct-horse-1", "new_password": "a-much-better-one"})
    assert r.status_code == 200 and r.json()["must_change_password"] is False
    assert client.get("/api/roster/me").status_code == 200


def test_password_change_logs_out_other_devices(source):
    add_staff("AB0003", "sonya@example.com")
    with TestClient(app, headers={"x-ab-app": "1"}) as phone, TestClient(app, headers={"x-ab-app": "1"}) as laptop:
        login(phone, "sonya@example.com")
        login(laptop, "sonya@example.com")
        phone.post("/api/auth/change-password",
                   json={"current_password": "correct-horse-1", "new_password": "a-much-better-one"})
        assert phone.get("/api/auth/me").status_code == 200
        assert laptop.get("/api/auth/me").status_code == 401


def test_staff_see_only_their_own_shifts(client, source):
    """HARD RULE (brief 9.3): no other staff member's data in a staff view."""
    add_staff("AB0003", "sonya@example.com")
    add_staff("AB0004", "jo@example.com")
    source.tabs = tabs_with("AB0003", "AB0004", "AB0004", "AB9999")
    login(client, "sonya@example.com")
    body = client.get("/api/roster/me").json()
    shifts = [s for m in body["months"] for s in m["shifts"]]
    assert len(shifts) == 1 and shifts[0]["staff_id"] == "AB0003"
    text = str(body)
    assert "AB0004" not in text and "AB9999" not in text and "issues" not in body
    assert client.get("/api/admin/roster").status_code == 403


def test_empty_month_returns_no_shifts_and_published_flag(client, source):
    add_staff("AB0003", "sonya@example.com")
    source.tabs = {month_tab(today()): [HEADER]}
    login(client, "sonya@example.com")
    months = client.get("/api/roster/me").json()["months"]
    assert [(m["published"], m["shifts"]) for m in months] == [(True, []), (False, [])]


def test_admin_sees_everything_and_issues(client, source):
    add_staff("AB0001", "brendan@example.com", role="admin")
    add_staff("AB0003", "sonya@example.com")
    source.tabs = tabs_with("AB0003", "AB9999")
    login(client, "brendan@example.com")
    body = client.get("/api/admin/roster").json()
    assert len(body["months"][0]["shifts"]) == 2
    [issue] = body["issues"]
    assert issue["level"] == "error" and "AB9999" in issue["message"]
    assert {s["staff_id"] for s in body["staff"]} == {"AB0001", "AB0003"}


def test_roster_unavailable_is_503(client, source):
    add_staff("AB0003", "sonya@example.com")
    source.fail = True
    login(client, "sonya@example.com")
    assert client.get("/api/roster/me").status_code == 503


def test_logout(client, source):
    add_staff("AB0003", "sonya@example.com")
    login(client, "sonya@example.com")
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_security_headers_and_static(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/api/auth/me").headers["cache-control"] == "no-store"
