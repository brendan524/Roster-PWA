"""Blue (blue.app) connection, one per staff member.

Each person connects their own Blue account once (OAuth 2.1 with PKCE). We keep their
refresh token encrypted on the server and refresh the 24-hour access token silently, so
there's no daily sign-in. Every Blue call is made with that person's own token, so Blue's
own permissions (including field-level rules) apply. The browser never sees a Blue token.

Blue's OAuth server (per https://blue.app/api/graphql/start-guide/authentication):
- endpoints are discovered from /.well-known/oauth-authorization-server
- clients register themselves (RFC 7591); only public clients, so no client secret
- PKCE S256 is required; codes are single-use
- refresh tokens rotate; the old one works for 60 s, and replaying it after that revokes the
  whole grant, so refreshes are serialised per person with a row lock
- a grant has no scopes: it can do anything the person can do in Blue. What this app may do
  is limited here instead: the browser can only reach the named operations in OPERATIONS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app import state as app_state
from app.config import get_settings
from app.models import BlueConnection

log = logging.getLogger(__name__)

TIMEOUT = 20
REFRESH_MARGIN = timedelta(minutes=5)

# The only GraphQL this app sends to Blue: read records, comments and checklists, add a
# comment, tick a checklist item. Nothing here deletes, archives or changes a workspace's
# structure, and the browser can't send queries of its own. (Tests check this.)
OPERATIONS = {
    "CurrentUser": "query CurrentUser { currentUser { id email fullName } }",
    "Organizations": """query Organizations {
  organizations(sort: [name_ASC], take: 100) { items { id name slug } }
}""",
    "Workspaces": """query Workspaces($orgIds: [String!]!) {
  workspaceList(filter: { companyIds: $orgIds, inProject: true, archived: false, isTemplate: false },
                sort: [position_ASC], take: 200) {
    items { id name slug color icon company { id name } folder { id title position color } }
  }
}""",
    # A workspace's lists (board columns), in Blue's board order.
    "Lists": """query Lists($workspaceId: String!, $workspaceKey: ID!) {
  recordLists(projectId: $workspaceId) { id title rank isDisabled }
  recordDatabases(workspaceId: $workspaceKey) { id name }
}""",
    # One list's records in board order: Blue only rank-sorts within exactly one list.
    "ListRecords": """query ListRecords($orgId: String!, $workspaceId: String!, $listId: String!) {
  recordQueries {
    todos(filter: { companyIds: [$orgId], projectIds: [$workspaceId], todoListIds: [$listId] }, sort: [rank_DESC], limit: 200) {
      items { ...Card }
      pageInfo { hasNextPage }
    }
  }
}
fragment Card on Record {
  id title done duedAt startedAt commentCount checklistCount checklistCompletedCount
  tags { id title color }
  users { id fullName }
}""",
    # Records outside every list (or all records, for a workspace with no lists).
    "UnlistedRecords": """query UnlistedRecords($orgId: String!, $workspaceId: String!, $notListIds: [String!]!) {
  recordQueries {
    todos(filter: { companyIds: [$orgId], projectIds: [$workspaceId], notTodoListIds: $notListIds }, sort: [createdAt_ASC], limit: 200) {
      items { ...Card }
      pageInfo { hasNextPage }
    }
  }
}
fragment Card on Record {
  id title done duedAt startedAt commentCount checklistCount checklistCompletedCount
  tags { id title color }
  users { id fullName }
}""",
    # Read with the workspace's database: Blue's database-style workspaces expect it.
    "Record": """query Record($id: String!, $databaseId: ID) {
  record(id: $id, databaseId: $databaseId) {
    id title text done duedAt startedAt
    todoList { id title }
    tags { id title color }
    users { id fullName }
    customFields { id name type value viewable }
    checklistPermissions { canEditChecklistItem }
    checklists { id title position checklistItems(orderBy: position_ASC) { id title done position } }
  }
}""",
    "Comments": """query Comments($id: ID!, $databaseId: ID!) {
  recordComments(recordId: $id, databaseId: $databaseId, first: 100) {
    entries { id text createdAt deletedAt author { id firstName lastName } }
  }
}""",
    # For a workspace without a database (older layout).
    "LegacyComments": """query LegacyComments($id: String!) {
  commentList(categoryId: $id, category: TODO, first: 100, orderBy: createdAt_ASC) {
    comments { id text createdAt user { id fullName } }
  }
}""",
    "AddComment": """mutation AddComment($id: String!, $html: String!, $text: String!) {
  createComment(input: { category: TODO, categoryId: $id, html: $html, text: $text }) {
    id text createdAt user { id fullName }
  }
}""",
    "TickChecklistItem": """mutation TickChecklistItem($id: String!, $done: Boolean!) {
  editChecklistItem(input: { checklistItemId: $id, done: $done }) { id done }
}""",
}


class BlueError(RuntimeError):
    """Blue couldn't be reached or refused a request."""


class BlueReconnect(BlueError):
    """This person's Blue connection is gone (revoked, expired or unreadable): they need to connect again."""


class BlueNotConnected(BlueReconnect):
    """This person has never connected Blue (or disconnected it themselves)."""


# ---- token encryption (key derived from the server secret, never stored in the DB) ----

def _fernet() -> Fernet:
    pepper = get_settings().pin_pepper
    if not pepper:
        raise BlueError("PIN_PEPPER is not set")
    key = hmac.new(pepper.encode(), b"blue-token-encryption", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enc(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _dec(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise BlueReconnect("Stored Blue tokens can't be read (server secret changed?).") from e


# ---- discovery and client registration --------------------------------------------------

def metadata(http) -> dict:
    """Blue's authorization-server metadata (RFC 8414)."""
    r = http.get(get_settings().blue_oauth_metadata_url, timeout=TIMEOUT)
    if r.status_code != 200:
        raise BlueError(f"Couldn't reach Blue ({r.status_code}).")
    meta = r.json()
    for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not meta.get(k):
            raise BlueError(f"Blue's sign-in settings are missing {k}.")
    if "S256" not in meta.get("code_challenge_methods_supported", ["S256"]):
        raise BlueError("Blue no longer offers PKCE S256.")
    return meta


def client_id(db: Session, http, meta: dict, redirect_uri: str) -> str:
    """Our OAuth client id for this redirect URI, registering with Blue the first time.
    Registration is rate-limited by Blue, so it happens once and is remembered."""
    key = "blue_client:" + hashlib.sha256(f"{meta.get('issuer', '')}|{redirect_uri}".encode()).hexdigest()[:40]
    existing = app_state.get(db, key)
    if existing:
        return existing
    r = http.post(meta["registration_endpoint"], timeout=TIMEOUT, json={
        "client_name": "AB Group staff app",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    if r.status_code not in (200, 201):
        raise BlueError(f"Couldn't register the app with Blue ({r.status_code}): {r.text[:200]}")
    cid = r.json()["client_id"]
    app_state.put(db, key, cid)
    db.commit()
    return cid


# ---- authorization ------------------------------------------------------------------------

def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(meta: dict, cid: str, redirect_uri: str, state: str, challenge: str) -> str:
    return meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def _token_request(http, meta: dict, data: dict) -> dict:
    r = http.post(meta["token_endpoint"], data=data, timeout=TIMEOUT)
    if r.status_code != 200:
        err = ""
        try:
            err = r.json().get("error", "")
        except Exception:  # noqa: BLE001
            pass
        if r.status_code in (400, 401) and err in ("invalid_grant", "invalid_client", "unauthorized_client", ""):
            raise BlueReconnect(f"Blue refused the sign-in ({r.status_code} {err}).")
        raise BlueError(f"Blue sign-in failed ({r.status_code}).")
    tokens = r.json()
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise BlueError("Blue didn't return the expected tokens.")
    return tokens


def _store_tokens(conn: BlueConnection, tokens: dict) -> None:
    conn.access_token_enc = _enc(tokens["access_token"])
    conn.refresh_token_enc = _enc(tokens["refresh_token"])
    conn.expires_at = datetime.now(UTC) + timedelta(seconds=int(tokens.get("expires_in", 86400)))


def connect(db: Session, http, staff_id: str, code: str, verifier: str, redirect_uri: str) -> BlueConnection:
    meta = metadata(http)
    cid = client_id(db, http, meta, redirect_uri)
    tokens = _token_request(http, meta, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": cid, "code_verifier": verifier,
    })
    me = _graphql(http, tokens["access_token"], "CurrentUser")["currentUser"]
    conn = db.get(BlueConnection, staff_id) or BlueConnection(staff_id=staff_id)
    _store_tokens(conn, tokens)
    conn.client_id = cid
    conn.blue_user_id = me["id"]
    conn.blue_name = (me.get("fullName") or "")[:200]
    conn.blue_email = (me.get("email") or "")[:254]
    conn.connected_at = datetime.now(UTC)
    db.add(conn)
    db.commit()
    return conn


def disconnect(db: Session, http, staff_id: str) -> None:
    conn = db.get(BlueConnection, staff_id)
    if conn is None:
        return
    try:
        meta = metadata(http)
        if meta.get("revocation_endpoint"):
            http.post(meta["revocation_endpoint"], timeout=TIMEOUT,
                      data={"token": _dec(conn.refresh_token_enc), "token_type_hint": "refresh_token",
                            "client_id": conn.client_id})
    except Exception:  # noqa: BLE001 — still forget it locally
        log.warning("Blue token revoke failed for %s", staff_id, exc_info=True)
    db.delete(conn)
    db.commit()


def _forget(db: Session, staff_id: str) -> None:
    db.rollback()
    db.query(BlueConnection).filter_by(staff_id=staff_id).delete()
    db.commit()


def access_token(db: Session, http, staff_id: str, force_refresh: bool = False) -> str:
    """A valid access token for this person, refreshing (and rotating) if needed.
    The row lock means two requests at once can't both spend the same refresh token."""
    conn = db.query(BlueConnection).filter_by(staff_id=staff_id).with_for_update().first()
    if conn is None:
        db.rollback()
        raise BlueNotConnected("Blue isn't connected.")
    if not force_refresh and conn.expires_at > datetime.now(UTC) + REFRESH_MARGIN:
        token = _dec(conn.access_token_enc)
        db.commit()
        return token
    try:
        tokens = _token_request(http, metadata(http), {
            "grant_type": "refresh_token", "refresh_token": _dec(conn.refresh_token_enc), "client_id": conn.client_id,
        })
    except BlueReconnect:
        _forget(db, staff_id)
        raise
    except BlueError:
        db.rollback()
        raise
    _store_tokens(conn, tokens)
    db.commit()
    return tokens["access_token"]


# ---- GraphQL --------------------------------------------------------------------------------

def _graphql(http, token: str, op: str, variables: dict | None = None, headers: dict | None = None,
             partial: bool = False) -> dict:
    """partial=True: if Blue returns some data alongside errors (e.g. one field it won't share
    with this person), keep the data; the errors are listed under "_errors"."""
    if op not in OPERATIONS:
        raise ValueError(f"Unknown Blue operation {op}")
    r = http.post(get_settings().blue_api_url, timeout=TIMEOUT,
                  json={"operationName": op, "query": OPERATIONS[op], "variables": variables or {}},
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})})
    if r.status_code == 401:
        raise _Unauthenticated()
    if r.status_code == 429:
        raise BlueError("Blue is busy right now. Try again in a minute.")
    if r.status_code != 200:
        raise BlueError(f"Blue request failed ({r.status_code}).")
    body = r.json()
    errors = body.get("errors") or []
    if any((e.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for e in errors):
        raise _Unauthenticated()
    data = body.get("data") or {}
    if errors and partial and any(v is not None for v in data.values()):
        data["_errors"] = [{"path": e.get("path"), "code": (e.get("extensions") or {}).get("code"),
                            "message": str(e.get("message", ""))[:200]} for e in errors]
        log.info("Blue %s partial result: %s", op, data["_errors"])
        return data
    if errors:
        log.info("Blue %s refused: %s", op, [(e.get("path"), (e.get("extensions") or {}).get("code"), str(e.get("message", ""))[:200]) for e in errors])
        codes = {(e.get("extensions") or {}).get("code") for e in errors}
        if "FORBIDDEN" in codes:
            raise BlueError("Blue says you don't have permission to do that.")
        raise BlueError("Blue couldn't do that: " + "; ".join(str(e.get("message", ""))[:120] for e in errors[:2]))
    return body.get("data") or {}


class _Unauthenticated(Exception):
    pass


def call(db: Session, http, staff_id: str, op: str, variables: dict | None = None, headers: dict | None = None,
         partial: bool = False) -> dict:
    """Run one of OPERATIONS as this person. If Blue rejects the token, refresh once; if it's
    still rejected (e.g. they disconnected the app in Blue), forget it so they're asked to reconnect."""
    for attempt in (0, 1):
        token = access_token(db, http, staff_id, force_refresh=attempt == 1)
        try:
            return _graphql(http, token, op, variables, headers, partial)
        except _Unauthenticated:
            if attempt == 1:
                _forget(db, staff_id)
                raise BlueReconnect("Blue no longer accepts this connection.") from None
    raise AssertionError("unreachable")


# The sign-in in progress (anti-forgery state + PKCE verifier) rides in a short-lived,
# HTTP-only cookie. Both are URL-safe base64, so "." can't appear inside either.
def call_many(db: Session, http, staff_id: str, calls: list[tuple[str, dict]], headers: dict | None = None,
              workers: int = 6) -> list[dict]:
    """Run several OPERATIONS as this person side by side (one token, fetched once).
    If Blue rejects the token, fall back to one-by-one calls, which refresh or ask to reconnect."""
    from concurrent.futures import ThreadPoolExecutor

    if not calls:
        return []
    token = access_token(db, http, staff_id)
    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(calls))) as pool:
            return list(pool.map(lambda c: _graphql(http, token, c[0], c[1], headers), calls))
    except _Unauthenticated:
        return [call(db, http, staff_id, op, v, headers) for op, v in calls]


def pack_pending(state: str, verifier: str) -> str:
    return f"{state}.{verifier}"


def unpack_pending(raw: str | None) -> tuple[str, str] | None:
    parts = (raw or "").split(".")
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None
