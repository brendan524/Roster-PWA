from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app import xero
from app.api import xero as xero_api
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import Staff, XeroConnection, XeroEmployeeLink, XeroLeaveBalance
from tests.conftest import add_staff, login


class R:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data


class FakeXero:
    """Xero identity + AU payroll, with one-time-use refresh tokens."""

    def __init__(self, employees):
        self.employees = employees  # id -> dict(first, last, email, balances)
        self.n = 0
        self.valid_refresh = set()
        self.valid_access = set()
        self.payroll_calls = 0
        self.reject_next_access = False

    def _issue(self):
        self.n += 1
        a, r = f"access{self.n}", f"refresh{self.n}"
        self.valid_access.add(a)
        self.valid_refresh.add(r)
        return {"access_token": a, "refresh_token": r, "expires_in": 1800}

    def post(self, url, data=None, auth=None, timeout=None):
        if url == xero.REVOKE_URL:
            self.valid_refresh.discard(data["token"])
            return R(200, {})
        assert url == xero.TOKEN_URL and auth == ("cid", "secret")
        if data["grant_type"] == "authorization_code":
            return R(200, self._issue()) if data["code"] == "good-code" else R(400, text="invalid_grant")
        if data["refresh_token"] not in self.valid_refresh:
            return R(400, text='{"error":"invalid_grant"}')
        self.valid_refresh.discard(data["refresh_token"])  # rotation: each refresh token works once
        return R(200, self._issue())

    def get(self, url, params=None, headers=None, timeout=None):
        token = headers["Authorization"].split()[1]
        if url == xero.CONNECTIONS_URL:
            return R(200, [{"tenantId": "T1", "tenantType": "ORGANISATION", "tenantName": "AB Corporate Fitness"}])
        assert headers["Xero-tenant-id"] == "T1"
        if self.reject_next_access:
            self.reject_next_access = False
            return R(401, text="expired")
        if token not in self.valid_access:
            return R(401, text="bad token")
        self.payroll_calls += 1
        path = url.split("/payroll.xro/1.0/")[1]
        if path == "Employees":
            emps = [{"EmployeeID": i, "FirstName": e["first"], "LastName": e["last"], "Email": e.get("email"),
                     "Status": "ACTIVE"} for i, e in self.employees.items()]
            page = params.get("page", 1)
            return R(200, {"Employees": emps[(page - 1) * 100: page * 100]})
        eid = path.split("/")[1]
        e = self.employees[eid]
        return R(200, {"Employees": [{"EmployeeID": eid, "StartDate": e.get("start"),
                                      "TaxDeclaration": {"EmploymentBasis": e.get("basis", "PARTTIME")}, "LeaveBalances": [
            {"LeaveName": n, "LeaveTypeID": f"{eid}-{n}", "NumberOfUnits": u, "TypeOfUnits": "Hours"}
            for n, u in e["balances"].items()]}]})


@pytest.fixture
def fake(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "xero_client_id", "cid")
    monkeypatch.setattr(s, "xero_client_secret", "secret")
    monkeypatch.setattr(s, "xero_redirect_uri", "https://hr.example/api/admin/xero/callback")
    f = FakeXero({
        "E1": {"first": "Ab0010", "last": "", "start": "/Date(1704067200000+0000)/",  # 1 Jan 2024
               "balances": {"Annual Leave": 52.5, "Personal/Carer's Leave": 30.4, "Long Service Leave": 40,
                            "MYOB Annual Leave": 12}},
        "E2": {"first": "Somebody", "last": "Else", "email": "jody@example.com", "balances": {"Annual Leave": 8}},
        "E3": {"first": "Twin", "last": "Name", "balances": {"Annual Leave": 1}},
        "E4": {"first": "Twin", "last": "Name", "balances": {"Annual Leave": 2}},
    })
    app.dependency_overrides[xero_api.get_http] = lambda: f
    yield f
    app.dependency_overrides.pop(xero_api.get_http, None)
    xero._last_employees = []


def connect(client):
    r = client.get("/api/admin/xero/connect", follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["scope"] == [xero.SCOPES] and q["redirect_uri"] == ["https://hr.example/api/admin/xero/callback"]
    return client.get(f"/api/admin/xero/callback?code=good-code&state={q['state'][0]}", follow_redirects=False)


@pytest.fixture
def admin(client, source, fake):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None)  # name "Ab0010" matches Xero E1 by name
    add_staff("AB0016", "jody@example.com")  # matches E2 by email
    add_staff("AB0099", None)  # no match
    with SessionLocal() as db:
        db.get(Staff, "AB0099").name = "Twin Name"  # two Xero people with this name: must not guess
        db.commit()
    login(client, "AB0001")
    return client


def test_connect_sync_and_auto_link(admin, fake):
    r = connect(admin)
    assert r.status_code == 303 and r.headers["location"] == "/?xero=connected"
    with SessionLocal() as db:
        conn = db.get(XeroConnection, 1)
        assert conn.tenant_name == "AB Corporate Fitness"
        assert "refresh" not in conn.refresh_token_enc and "access" not in conn.access_token_enc  # encrypted
        links = {l.staff_id: l.xero_employee_id for l in db.query(XeroEmployeeLink)}
        assert links == {"AB0010": "E1", "AB0016": "E2"}  # AB0099 ambiguous -> left for an admin
        bals = {(b.staff_id, b.leave_name): float(b.units) for b in db.query(XeroLeaveBalance)}
        assert bals[("AB0010", "Annual Leave")] == 52.5
    status = admin.get("/api/admin/xero/status").json()
    assert status["connected"] and status["last_error"] == ""
    assert len(status["xero_employees"]) == 4


def test_callback_rejects_bad_state_and_cancel(admin, fake):
    admin.get("/api/admin/xero/connect", follow_redirects=False)
    r = admin.get("/api/admin/xero/callback?code=good-code&state=forged", follow_redirects=False)
    assert r.headers["location"] == "/?xero=failed"
    r = admin.get("/api/admin/xero/callback?error=access_denied&state=x", follow_redirects=False)
    assert r.headers["location"] == "/?xero=cancelled"
    with SessionLocal() as db:
        assert db.get(XeroConnection, 1) is None


def test_expired_access_token_is_refreshed_and_rotated(admin, fake):
    connect(admin)
    with SessionLocal() as db:
        db.get(XeroConnection, 1).expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert admin.post("/api/admin/xero/sync").status_code == 200
    assert admin.post("/api/admin/xero/sync").status_code == 200  # works again with the rotated token
    assert len(fake.valid_refresh) == 1  # old refresh tokens were used up, only the newest is valid


def test_unexpected_401_triggers_one_refresh_and_retry(admin, fake):
    connect(admin)
    fake.reject_next_access = True
    assert admin.post("/api/admin/xero/sync").status_code == 200


def test_revoked_connection_reports_reconnect(admin, fake):
    connect(admin)
    fake.valid_refresh.clear()  # e.g. disconnected in Xero, or unused for 60 days
    with SessionLocal() as db:
        db.get(XeroConnection, 1).expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    r = admin.post("/api/admin/xero/sync")
    assert r.status_code == 502 and "reconnect" in r.json()["detail"].lower()
    assert "reconnect" in admin.get("/api/admin/xero/status").json()["last_error"].lower()


def test_manual_link_and_move(admin, fake):
    connect(admin)
    assert admin.post("/api/admin/xero/link", json={"staff_id": "AB0099", "xero_employee_id": "E4"}).status_code == 200
    admin.post("/api/admin/xero/sync")
    with SessionLocal() as db:
        assert float(db.query(XeroLeaveBalance).filter_by(staff_id="AB0099").one().units) == 2
    # Move E1 from AB0010 to AB0099: AB0010 loses the link and its copied balances.
    admin.post("/api/admin/xero/link", json={"staff_id": "AB0099", "xero_employee_id": "E1"})
    with SessionLocal() as db:
        assert db.query(XeroEmployeeLink).filter_by(staff_id="AB0010").first() is None
        assert db.query(XeroLeaveBalance).filter_by(staff_id="AB0010").count() == 0
    assert admin.post("/api/admin/xero/link", json={"staff_id": "AB0099", "xero_employee_id": "nope"}).status_code == 400


def test_staff_see_own_xero_balances_only(admin, fake):
    connect(admin)
    admin.post("/api/auth/logout")
    login(admin, "AB0010")
    me = admin.get("/api/leave/me").json()
    # Long service is hidden until 7 years' service (started Jan 2024).
    assert {b["name"]: b["units"] for b in me["xero_balances"]} == {"Annual Leave": 52.5, "Personal/Carer's Leave": 30.4}
    assert "long_service" not in [t["code"] for t in me["types"]]
    assert "Somebody" not in str(me)
    assert admin.get("/api/admin/xero/status").status_code == 403
    assert admin.get("/api/admin/xero/connect", follow_redirects=False).status_code == 403


def test_approver_sees_matching_xero_balance(admin, fake):
    connect(admin)
    admin.post("/api/auth/logout")
    login(admin, "AB0010")
    admin.post("/api/leave/requests", json={"leave_type": "annual", "start_date": "2027-01-04", "end_date": "2027-01-04",
                                                   "reason": "Holiday"})
    admin.post("/api/auth/logout")
    login(admin, "AB0001")
    [p] = admin.get("/api/admin/leave").json()["pending"]
    assert p["balance_note"].startswith("Xero balance: 52.5 hours of Annual Leave")


def test_disconnect(admin, fake):
    connect(admin)
    assert admin.post("/api/admin/xero/disconnect").status_code == 200
    with SessionLocal() as db:
        assert db.get(XeroConnection, 1) is None and db.query(XeroLeaveBalance).count() == 0
    assert not fake.valid_refresh  # revoked at Xero too


def test_not_configured(client, source):
    add_staff("AB0001", None, role="admin")
    login(client, "AB0001")
    assert client.get("/api/admin/xero/connect", follow_redirects=False).status_code == 400
    assert client.get("/api/admin/xero/status").json()["configured"] is False


def test_long_service_hidden_until_seven_years(admin, fake):
    connect(admin)
    with SessionLocal() as db:
        assert db.get(XeroEmployeeLink, "AB0010").start_date == date(2024, 1, 1)
    row = next(s for s in admin.get("/api/admin/xero/status").json()["staff"] if s["staff_id"] == "AB0010")
    assert "Long Service Leave" in [b["name"] for b in row["balances"]]  # admins always see it
    admin.post("/api/auth/logout")
    login(admin, "AB0010")
    r = admin.post("/api/leave/requests", json={"leave_type": "long_service", "start_date": "2027-01-04",
                                                "end_date": "2027-01-04", "reason": "Trip"})
    assert r.status_code == 400
    fake.employees["E1"]["start"] = "2019-06-01T00:00:00"  # over 7 years ago
    admin.post("/api/auth/logout")
    login(admin, "AB0001")
    admin.post("/api/admin/xero/sync")
    admin.post("/api/auth/logout")
    login(admin, "AB0010")
    me = admin.get("/api/leave/me").json()
    assert "Long Service Leave" in [b["name"] for b in me["xero_balances"]]
    assert "long_service" in [t["code"] for t in me["types"]]


def test_contractors_have_no_leave(admin, fake):
    with SessionLocal() as db:
        db.get(Staff, "AB0016").has_leave = False
        db.commit()
    connect(admin)
    with SessionLocal() as db:
        assert db.get(XeroEmployeeLink, "AB0016") is None  # not auto-linked
    assert "AB0016" not in [s["staff_id"] for s in admin.get("/api/admin/xero/status").json()["staff"]]
    admin.post("/api/auth/logout")
    login(admin, "AB0016")
    assert admin.get("/api/auth/me").json()["has_leave"] is False
    me = admin.get("/api/leave/me").json()
    assert me["has_leave"] is False and me["types"] == [] and me["xero_balances"] is None
    r = admin.post("/api/leave/requests", json={"leave_type": "unpaid", "start_date": "2027-01-04",
                                                "end_date": "2027-01-04", "reason": "Away"})
    assert r.status_code == 400 and "Contractors" in r.json()["detail"]


@pytest.mark.parametrize("value,expected", [
    ("/Date(1704067200000+0000)/", date(2024, 1, 1)), ("/Date(1704067200000)/", date(2024, 1, 1)),
    ("2019-06-01T00:00:00", date(2019, 6, 1)), (None, None), ("junk", None),
])
def test_parse_xero_date(value, expected):
    assert xero.parse_xero_date(value) == expected


def test_myob_balances_never_shown_and_casuals_have_none(admin, fake):
    fake.employees["E2"]["basis"] = "CASUAL"
    connect(admin)
    with SessionLocal() as db:
        names = {(b.staff_id, b.leave_name) for b in db.query(XeroLeaveBalance)}
    assert ("AB0010", "MYOB Annual Leave") not in names and ("AB0010", "Annual Leave") in names
    assert not any(sid == "AB0016" for sid, _ in names)  # casual: nothing copied
    row = next(s for s in admin.get("/api/admin/xero/status").json()["staff"] if s["staff_id"] == "AB0016")
    assert row["link"]["casual"] is True and row["balances"] == []
    admin.post("/api/auth/logout")
    login(admin, "AB0016")
    me = admin.get("/api/leave/me").json()
    assert me["casual"] is True and [t["code"] for t in me["types"]] == ["unpaid"]
    assert admin.post("/api/leave/requests", json={"leave_type": "annual", "start_date": "2027-01-04",
                                                   "end_date": "2027-01-04", "reason": "Holiday"}).status_code == 400
