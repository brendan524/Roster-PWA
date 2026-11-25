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
        self.calls = []  # (operationName, variables, headers, user)
        self.down = False
        # u1 is in two orgs; u2 only in one, and can't see the "Pay rate" field.
        self.orgs = {"u1": [{"id": "o1", "name": "AB Group", "slug": "ab"}, {"id": "o2", "name": "Side Org", "slug": "side"}],
                     "u2": [{"id": "o1", "name": "AB Group", "slug": "ab"}]}
        self.workspaces = {"u1": [{"id": "w1", "name": "First Aid Courses", "slug": "fa", "color": "#00f", "icon": None, "company": {"id": "o1", "name": "AB Group"}},
                                  {"id": "w2", "name": "Gym Tasks", "slug": "gym", "color": None, "icon": None, "company": {"id": "o1", "name": "AB Group"}}],
                           "u2": [{"id": "w2", "name": "Gym Tasks", "slug": "gym", "color": None, "icon": None, "company": {"id": "o1", "name": "AB Group"}}]}
        self.checklist_done = {"i1": False, "i2": True}
        self.comments = [{"id": "c1", "text": "Booked the room", "createdAt": "2026-11-20T01:00:00Z", "user": {"id": "u2", "fullName": "Dan Blue"}}]

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
        user, op, v = hit[0], json["operationName"], json.get("variables") or {}
        self.queries.append((op, json["query"], user))
        self.calls.append((op, v, headers, user))
        if op == "CurrentUser":
            return R(200, {"data": {"currentUser": self.users[user]}})
        if op == "Organizations":
            return R(200, {"data": {"organizations": {"items": self.orgs[user]}}})
        if op == "Workspaces":
            return R(200, {"data": {"workspaceList": {"items": [w for w in self.workspaces[user] if w["company"]["id"] in v["orgIds"]]}}})
        if op in ("Lists", "ListRecords", "UnlistedRecords"):
            if v["workspaceId"] not in {w["id"] for w in self.workspaces[user]}:
                return R(200, {"errors": [{"message": "nope", "extensions": {"code": "FORBIDDEN"}}]})
            # Blue: "A rank sort must be the only sort and the filter must resolve to exactly one list."
            if "rank_" in json["query"] and "todoListIds: [$listId]" not in json["query"]:
                return R(200, {"errors": [{"message": "Rank ordering requires exactly one record list.",
                                           "extensions": {"code": "BAD_USER_INPUT"}}]})
            card = lambda i, t, **kw: {"id": i, "title": t, "done": False, "duedAt": None, "startedAt": None, "commentCount": 0,
                                      "checklistCount": 0, "checklistCompletedCount": 0, "tags": [], "users": [], **kw}
            in_list = {  # already in Blue's board order (rank_DESC)
                "l1": [card("r1", "HLTAID011 — Acme", duedAt="2026-12-01T00:00:00Z", commentCount=1, checklistCount=2,
                            checklistCompletedCount=1, tags=[{"id": "t1", "title": "Onsite", "color": "#f00"}],
                            users=[{"id": "u1", "fullName": "Sonya Blue"}]),
                       card("r3", "Second in Mon")],
                "l2": [], "l3": [card("r9", "Hidden list record")]}
            if op == "Lists":
                return R(200, {"data": {"recordLists": [  # deliberately not in rank order
                    {"id": "l2", "title": "Tuesday ", "rank": "a1", "isDisabled": False},
                    {"id": "l3", "title": "Old", "rank": "a2", "isDisabled": True},
                    {"id": "l1", "title": "Monday ", "rank": "a0", "isDisabled": False}]}})
            if op == "ListRecords":
                return R(200, {"data": {"recordQueries": {"todos": {"items": in_list[v["listId"]], "pageInfo": {"hasNextPage": False}}}}})
            loose = [card("r2", "Loose record", done=True)] if set(v["notListIds"]) >= {"l1", "l2"} else []
            return R(200, {"data": {"recordQueries": {"todos": {"items": loose, "pageInfo": {"hasNextPage": False}}}}})
        if op == "Record":
            fields = [{"id": "f1", "name": "Venue", "type": "TEXT_SINGLE", "value": "Melton", "viewable": True},
                      {"id": "f2", "name": "Pay rate", "type": "CURRENCY", "value": 55, "viewable": user == "u1"}]
            return R(200, {"data": {
                "record": {"id": v["id"], "title": "HLTAID011 — Acme", "text": "Bring the manikins", "done": False,
                           "duedAt": "2026-12-01T00:00:00Z", "startedAt": None, "todoList": {"id": "l1", "title": "Mon 1 Dec"},
                           "tags": [], "users": [{"id": "u1", "fullName": "Sonya Blue"}], "customFields": fields,
                           "checklistPermissions": {"canEditChecklistItem": user == "u1"},
                           "checklists": [{"id": "k1", "title": "Before the course", "position": 1, "checklistItems": [
                               {"id": "i1", "title": "Pack kit", "done": self.checklist_done["i1"], "position": 1},
                               {"id": "i2", "title": "Confirm venue", "done": self.checklist_done["i2"], "position": 2}]}]},
                "commentList": {"comments": self.comments}}})
        if op == "AddComment":
            c = {"id": f"c{len(self.comments) + 1}", "text": v["text"], "createdAt": "2026-11-25T01:00:00Z",
                 "user": {"id": user, "fullName": self.users[user]["fullName"]}}
            self.comments.append(c)
            return R(200, {"data": {"createComment": c}})
        if op == "TickChecklistItem":
            if user != "u1":
                return R(200, {"errors": [{"message": "no", "extensions": {"code": "FORBIDDEN"}}]})
            self.checklist_done[v["id"]] = v["done"]
            return R(200, {"data": {"editChecklistItem": {"id": v["id"], "done": v["done"]}}})
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
    """Blue grants have no scopes, so this list is what limits the app. Reads are queries; the
    only changes allowed are adding a comment and ticking a checklist item. No deletes,
    archives, trash, or workspace/field/role changes, and nothing outside the list can be sent."""
    mutations = {}
    for name, query in blue.OPERATIONS.items():
        kind = query.split()[0]
        assert kind in ("query", "mutation"), name
        if kind == "mutation":
            mutations[name] = set(re.findall(r"\b([a-zA-Z]+)\s*\(input:", query))
    assert mutations == {"AddComment": {"createComment"}, "TickChecklistItem": {"editChecklistItem"}}
    banned = re.compile(r"\b(delete\w*|archiveRecord|trash\w*|remove\w*|\w*CustomField\b|create(Record|Workspace)\w*|"
                        r"edit(Workspace|Record|Comment)\b|\w*WorkspaceUserRole|inviteUser|\w*PersonalAccessToken)\s*\(", re.I)
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


H = {"x-ab-app": "1"}


def test_workspaces_lists_only_what_the_person_belongs_to(client, source, fake):
    add_staff("AB0003", "sonya@example.com")
    add_staff("AB0004", "dan@example.com")
    login(client, "sonya@example.com")
    connect(client, fake, "u1")
    body = client.get("/api/blue/workspaces").json()
    assert body == {"organizations": [{"id": "o1", "name": "AB Group", "workspaces": [
        {"id": "w1", "name": "First Aid Courses", "color": "#00f", "icon": None},
        {"id": "w2", "name": "Gym Tasks", "color": None, "icon": None}]}]}  # Side Org has none -> left out
    client.post("/api/auth/logout")
    login(client, "dan@example.com")
    connect(client, fake, "u2")
    names = [w["name"] for o in client.get("/api/blue/workspaces").json()["organizations"] for w in o["workspaces"]]
    assert names == ["Gym Tasks"]


def test_board_groups_records_into_lists(sonya, fake):
    connect(sonya, fake)
    b = sonya.get("/api/blue/workspaces/o1/w1").json()
    # Lists in Blue's order (by rank), disabled lists left out, titles trimmed; listless records last.
    assert [c["title"] for c in b["columns"]] == ["Monday", "Tuesday", "No list"]
    assert [r["id"] for r in b["columns"][0]["records"]] == ["r1", "r3"]  # Blue's top-to-bottom order
    assert b["columns"][1]["records"] == [] and b["truncated"] is False
    card = b["columns"][0]["records"][0]
    assert card == {"id": "r1", "title": "HLTAID011 — Acme", "done": False, "due": "2026-12-01T00:00:00Z", "start": None,
                    "comments": 1, "checklist": [1, 2], "tags": [{"title": "Onsite", "color": "#f00"}], "people": ["Sonya Blue"]}
    ops = [(op, v) for (op, v, _, _) in fake.calls if op in ("Lists", "ListRecords", "UnlistedRecords")]
    assert ops == [("Lists", {"workspaceId": "w1"}),
                   ("ListRecords", {"orgId": "o1", "workspaceId": "w1", "listId": "l1"}),
                   ("ListRecords", {"orgId": "o1", "workspaceId": "w1", "listId": "l2"}),
                   ("UnlistedRecords", {"orgId": "o1", "workspaceId": "w1", "notListIds": ["l1", "l2"]})]
    _, _, headers, user = fake.calls[-1]
    assert user == "u1" and headers["blue-org-id"] == "o1" and headers["blue-workspace-id"] == "w1"


def test_board_blue_refuses_shows_an_error_not_a_logout(sonya, fake):
    connect(sonya, fake, "u2")
    r = sonya.get("/api/blue/workspaces/o1/w1")
    assert r.status_code == 502 and "permission" in r.json()["detail"]
    assert sonya.get("/api/auth/me").status_code == 200


def test_record_shows_only_fields_blue_lets_them_see(client, source, fake):
    add_staff("AB0003", "sonya@example.com")
    add_staff("AB0004", "dan@example.com")
    login(client, "sonya@example.com")
    connect(client, fake, "u1")
    r = client.get("/api/blue/records/r1").json()
    assert [f["name"] for f in r["fields"]] == ["Venue", "Pay rate"]
    assert r["can_tick"] is True and r["checklists"][0]["items"][0] == {"id": "i1", "title": "Pack kit", "done": False}
    assert r["comments"] == [{"id": "c1", "text": "Booked the room", "at": "2026-11-20T01:00:00Z", "author": "Dan Blue"}]
    client.post("/api/auth/logout")
    login(client, "dan@example.com")
    connect(client, fake, "u2")
    r = client.get("/api/blue/records/r1").json()
    assert [f["name"] for f in r["fields"]] == ["Venue"]  # Blue says Dan can't view Pay rate
    assert r["can_tick"] is False


def test_comment_is_posted_as_the_person_with_escaped_html(sonya, fake):
    connect(sonya, fake)
    r = sonya.post("/api/blue/records/r1/comments", json={"text": "Room <b>booked</b>\nSee you there"}, headers=H)
    assert r.json()["author"] == "Sonya Blue" and r.json()["text"] == "Room <b>booked</b>\nSee you there"
    op, v, _, user = fake.calls[-1]
    assert op == "AddComment" and user == "u1" and v["id"] == "r1"
    assert v["html"] == "<p>Room &lt;b&gt;booked&lt;/b&gt;</p><p>See you there</p>"
    assert sonya.post("/api/blue/records/r1/comments", json={"text": "   "}, headers=H).status_code == 400


def test_tick_checklist_item_writes_back(client, source, fake):
    add_staff("AB0003", "sonya@example.com")
    login(client, "sonya@example.com")
    connect(client, fake, "u1")
    assert client.post("/api/blue/checklist-items/i1", json={"done": True}, headers=H).json() == {"id": "i1", "done": True}
    assert fake.checklist_done["i1"] is True
    assert client.get("/api/blue/records/r1").json()["checklists"][0]["items"][0]["done"] is True


def test_not_connected_or_revoked_is_409_reconnect(sonya, fake):
    r = sonya.get("/api/blue/workspaces")
    assert r.status_code == 409 and r.json()["detail"] == "Connect Blue to see your boards."
    connect(sonya, fake)
    fake.revoked_grants.update(g for (_, g) in fake.access.values())
    r = sonya.get("/api/blue/records/r1")
    assert r.status_code == 409 and r.json()["detail"] == "Please connect Blue again."


def test_ids_are_checked_before_going_to_blue(sonya, fake):
    connect(sonya, fake)
    n = len(fake.calls)
    assert sonya.get("/api/blue/records/r1%22%7D").status_code == 400
    assert sonya.post("/api/blue/checklist-items/x%20y", json={"done": True}, headers=H).status_code == 400
    assert len(fake.calls) == n
