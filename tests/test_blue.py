import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app import blue
from app import state as app_state
from app.api import blue as blue_api
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import BlueConnection
from tests.conftest import add_staff, login

META_URL = "https://api.blue.test/.well-known/oauth-authorization-server"
API_URL = "https://api.blue.test/graphql"
REDIRECT = "https://hr.example/api/blue/callback"
META = {
    "issuer": "https://mcp.blue.test",
    "authorization_endpoint": "https://mcp.blue.test/oauth2/authorize",
    "token_endpoint": "https://mcp.blue.test/oauth2/token",
    "registration_endpoint": "https://mcp.blue.test/oauth2/register",
    "revocation_endpoint": "https://mcp.blue.test/oauth2/revoke",
    "code_challenge_methods_supported": ["S256"],
}


class R:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data


class FakeBlue:
    """Blue's OAuth 2.1 server + GraphQL API, as documented: public clients registered
    dynamically, PKCE S256, single-use codes, rotating refresh tokens whose replay revokes the grant."""

    def __init__(self):
        self.registrations = 0
        self.n = 0
        self.codes = {}  # code -> (user, challenge, client_id, redirect_uri)
        self.refresh = {}  # refresh token -> (user, grant)
        self.spent = {}  # used refresh token -> grant
        self.access = {}  # access token -> (user, grant)
        self.revoked_grants = set()
        self.users = {"u1": {"id": "u1", "email": "sonya@blue.example", "fullName": "Sonya Blue"},
                      "u2": {"id": "u2", "email": "dan@blue.example", "fullName": "Dan Blue"}}
        self.queries = []  # (operationName, query, user)
        self.down = False

    def authorize(self, url, user):
        """What Blue does when the person approves: hands back a code for our callback."""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert url.startswith(META["authorization_endpoint"])
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        self.n += 1
        code = f"code{self.n}"
        self.codes[code] = (user, q["code_challenge"], q["client_id"], q["redirect_uri"])
        return code, q["state"]

    def _issue(self, user, grant):
        self.n += 1
        a, r = f"acc{self.n}", f"ref{self.n}"
        self.access[a] = (user, grant)
        self.refresh[r] = (user, grant)
        return {"access_token": a, "refresh_token": r, "token_type": "Bearer", "expires_in": 86400}

    def get(self, url, timeout=None):
        if self.down:
            return R(503, text="down")
        assert url == META_URL
        return R(200, META)

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if self.down:
            return R(503, text="down")
        if url == META["registration_endpoint"]:
            assert json["token_endpoint_auth_method"] == "none" and json["redirect_uris"] == [REDIRECT]
            self.registrations += 1
            return R(201, {"client_id": f"client{self.registrations}"})
        if url == META["revocation_endpoint"]:
            hit = self.refresh.pop(data["token"], None)
            if hit:
                self.revoked_grants.add(hit[1])
            return R(200, {})
        if url == META["token_endpoint"]:
            assert "client_secret" not in data  # public client
            if data["grant_type"] == "authorization_code":
                hit = self.codes.pop(data["code"], None)  # single use
                if not hit:
                    return R(400, {"error": "invalid_grant"})
                user, challenge, cid, redirect = hit
                verifier_hash = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if verifier_hash != challenge or cid != data["client_id"] or redirect != data["redirect_uri"]:
                    return R(400, {"error": "invalid_grant"})
                self.n += 1
                return R(200, self._issue(user, f"grant{self.n}"))
            assert data["grant_type"] == "refresh_token"
            tok = data["refresh_token"]
            if tok in self.spent:  # replay outside the grace window revokes the whole grant
                self.revoked_grants.add(self.spent[tok])
                return R(400, {"error": "invalid_grant"})
            hit = self.refresh.pop(tok, None)
            if not hit or hit[1] in self.revoked_grants:
                return R(400, {"error": "invalid_grant"})
            self.spent[tok] = hit[1]
            return R(200, self._issue(*hit))
        assert url == API_URL
        auth = (headers or {}).get("Authorization", "")
        hit = self.access.get(auth.removeprefix("Bearer "))
        if not hit or hit[1] in self.revoked_grants:
            return R(200, {"errors": [{"message": "Not authenticated", "extensions": {"code": "UNAUTHENTICATED"}}]})
        self.queries.append((json["operationName"], json["query"], hit[0]))
        if json["operationName"] == "CurrentUser":
            return R(200, {"data": {"currentUser": self.users[hit[0]]}})
        return R(400, text="unknown")


@pytest.fixture
def fake(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "blue_oauth_metadata_url", META_URL)
    monkeypatch.setattr(s, "blue_api_url", API_URL)
    monkeypatch.setattr(s, "blue_redirect_uri", REDIRECT)
    f = FakeBlue()
    app.dependency_overrides[blue_api.get_http] = lambda: f
    yield f
    app.dependency_overrides.pop(blue_api.get_http, None)


@pytest.fixture
def sonya(client, source, fake):
    add_staff("AB0003", "sonya@example.com")
    login(client, "sonya@example.com")
    return client


def connect(client, fake, user="u1"):
    r = client.get("/api/blue/connect", follow_redirects=False)
    assert r.status_code == 303
    code, state = fake.authorize(r.headers["location"], user)
    return client.get(f"/api/blue/callback?code={code}&state={state}", follow_redirects=False)


def test_not_connected_then_connect_shows_blue_name(sonya, fake):
    assert sonya.get("/api/blue/status").json() == {"enabled": True, "connected": False}
    r = connect(sonya, fake)
    assert r.headers["location"] == "/?blue=connected"
    st = sonya.get("/api/blue/status").json()
    assert st == {"enabled": True, "connected": True, "name": "Sonya Blue", "email": "sonya@blue.example"}
    with SessionLocal() as db:
        conn = db.get(BlueConnection, "AB0003")
        assert conn.blue_user_id == "u1"
        assert "ref" not in conn.refresh_token_enc and "acc" not in conn.access_token_enc  # encrypted at rest


def test_authorize_request_uses_pkce_and_registers_once(sonya, fake):
    r = sonya.get("/api/blue/connect", follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["redirect_uri"] == REDIRECT and q["client_id"] == "client1"
    sonya.get("/api/blue/connect", follow_redirects=False)
    assert fake.registrations == 1  # remembered, not re-registered (Blue rate-limits registration)


def test_no_blue_token_ever_reaches_the_browser(sonya, fake):
    responses = [connect(sonya, fake)]
    responses.append(sonya.get("/api/blue/status"))
    for r in responses:
        blob = r.text + str(dict(r.headers))
        assert not re.search(r"\b(acc|ref)\d+\b", blob), blob
    # The pending cookie (state + PKCE verifier) is HTTP-only and scoped to /api/blue.
    r = sonya.get("/api/blue/connect", follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/api/blue" in cookie and "Max-Age=600" in cookie


def test_callback_rejects_forged_state_wrong_verifier_and_cancel(sonya, fake):
    r = sonya.get("/api/blue/connect", follow_redirects=False)
    code, _ = fake.authorize(r.headers["location"], "u1")
    assert sonya.get(f"/api/blue/callback?code={code}&state=forged", follow_redirects=False).headers["location"] == "/?blue=failed"
    r = sonya.get("/api/blue/callback?error=access_denied&state=x", follow_redirects=False)
    assert r.headers["location"] == "/?blue=cancelled"
    # A code issued for a different sign-in (so a different PKCE verifier) is refused by Blue.
    r = sonya.get("/api/blue/connect", follow_redirects=False)
    code, state = fake.authorize(r.headers["location"], "u1")
    user, _, cid, redirect = fake.codes[code]
    fake.codes[code] = (user, "challenge-from-another-sign-in", cid, redirect)
    assert sonya.get(f"/api/blue/callback?code={code}&state={state}", follow_redirects=False).headers["location"] == "/?blue=failed"
    with SessionLocal() as db:
        assert db.get(BlueConnection, "AB0003") is None


def test_each_person_calls_blue_as_themselves(client, source, fake):
    add_staff("AB0003", "sonya@example.com")
    add_staff("AB0004", "dan@example.com")
    login(client, "sonya@example.com")
    connect(client, fake, "u1")
    client.post("/api/auth/logout")
    login(client, "dan@example.com")
    connect(client, fake, "u2")
    assert client.get("/api/blue/status").json()["name"] == "Dan Blue"
    client.post("/api/auth/logout")
    login(client, "sonya@example.com")
    assert client.get("/api/blue/status").json()["name"] == "Sonya Blue"
    assert [u for (_, _, u) in fake.queries][-2:] == ["u2", "u1"]


def test_expired_access_token_refreshes_silently_and_rotates(sonya, fake):
    connect(sonya, fake)
    with SessionLocal() as db:
        conn = db.get(BlueConnection, "AB0003")
        old_refresh = blue._dec(conn.refresh_token_enc)
        conn.expires_at = datetime.now(UTC) + timedelta(minutes=1)  # inside the refresh margin
        db.commit()
    assert sonya.get("/api/blue/status").json()["connected"] is True
    with SessionLocal() as db:
        conn = db.get(BlueConnection, "AB0003")
        assert blue._dec(conn.refresh_token_enc) != old_refresh  # rotated and saved
        assert conn.expires_at > datetime.now(UTC) + timedelta(hours=23)
    assert old_refresh in fake.spent


def test_failed_refresh_asks_to_reconnect(sonya, fake):
    connect(sonya, fake)
    with SessionLocal() as db:
        conn = db.get(BlueConnection, "AB0003")
        fake.revoked_grants.add(fake.refresh[blue._dec(conn.refresh_token_enc)][1])
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert sonya.get("/api/blue/status").json() == {"enabled": True, "connected": False, "reconnect": True}
    with SessionLocal() as db:
        assert db.get(BlueConnection, "AB0003") is None  # forgotten, so the next tap reconnects cleanly
    assert connect(sonya, fake).headers["location"] == "/?blue=connected"


def test_app_removed_in_blue_asks_to_reconnect(sonya, fake):
    """Disconnected under Blue's Connected apps: the access token stops working at once."""
    connect(sonya, fake)
    fake.revoked_grants.update(g for (_, g) in fake.access.values())
    assert sonya.get("/api/blue/status").json()["reconnect"] is True


def test_blue_down_keeps_the_connection(sonya, fake):
    connect(sonya, fake)
    with SessionLocal() as db:
        db.get(BlueConnection, "AB0003").expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    fake.down = True
    st = sonya.get("/api/blue/status").json()
    assert st["connected"] is True and st["name"] == "Sonya Blue" and "warning" in st
    with SessionLocal() as db:
        assert db.get(BlueConnection, "AB0003") is not None


def test_disconnect_revokes_and_forgets(sonya, fake):
    connect(sonya, fake)
    assert sonya.post("/api/blue/disconnect", headers={"x-ab-app": "1"}).json() == {"ok": True}
    assert fake.revoked_grants
    assert sonya.get("/api/blue/status").json()["connected"] is False


def test_needs_login_and_csrf_header(client, source, fake):
    assert client.get("/api/blue/status").status_code == 401
    assert client.get("/api/blue/connect", follow_redirects=False).status_code == 401
    add_staff("AB0003", "sonya@example.com")
    login(client, "sonya@example.com")
    assert client.post("/api/blue/disconnect", headers={"x-ab-app": ""}).status_code == 403  # no app header


def test_only_safe_operations_can_be_sent():
    """Blue grants have no scopes, so this list is what limits the app. No deletes, archives,
    trash or workspace/field/role changes; and nothing outside the list can be sent."""
    banned = re.compile(r"delete|archive|trash|remove|createCustomField|editCustomField|createRecordField|"
                        r"WorkspaceUserRole|inviteUser|createWorkspace|editWorkspace|PersonalAccessToken", re.I)
    for name, query in blue.OPERATIONS.items():
        assert not banned.search(query), name
    with pytest.raises(ValueError):
        blue._graphql(None, "t", "deleteRecord")


def test_client_registration_is_remembered_per_redirect(fake, monkeypatch):
    with SessionLocal() as db:
        a = blue.client_id(db, fake, META, REDIRECT)
        b = blue.client_id(db, fake, META, REDIRECT)
        assert a == b and fake.registrations == 1
        assert any(k.startswith("blue_client:") for k in [r.key for r in db.query(app_state.AppState)])
