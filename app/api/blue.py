"""Each staff member's own Blue connection: connect, status, disconnect.

No Blue token or secret is ever sent to the browser; it only learns whether the person is
connected and the name on their Blue account.
"""

import html
import logging
import re
import secrets

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import blue
from app.config import get_settings
from app.db import get_db
from app.deps import ready_staff
from app.models import BlueConnection, Staff

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/blue", tags=["blue"])

PENDING_COOKIE = "ab_blue_pending"


def get_http():
    """Overridden in tests with a fake Blue."""
    return requests.Session()


def _redirect_uri(request: Request) -> str:
    return get_settings().blue_redirect_uri or str(request.url_for("blue_callback"))


def _enabled() -> None:
    if not get_settings().blue_enabled:
        raise HTTPException(404, "Blue isn't turned on.")


@router.get("/status")
def status(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    """Connected or not, and the Blue account's name. Checks with Blue (which also keeps the
    token fresh), so a connection that was revoked in Blue shows up here as needing a reconnect."""
    if not get_settings().blue_enabled:
        return {"enabled": False, "connected": False}
    conn = db.get(BlueConnection, staff.staff_id)
    if conn is None:
        return {"enabled": True, "connected": False}
    try:
        me = blue.call(db, http, staff.staff_id, "CurrentUser")["currentUser"]
    except blue.BlueReconnect:
        return {"enabled": True, "connected": False, "reconnect": True}
    except blue.BlueError as e:
        # Blue unreachable for a moment: still connected, show what we know.
        return {"enabled": True, "connected": True, "name": conn.blue_name, "email": conn.blue_email,
                "warning": str(e)}
    conn = db.get(BlueConnection, staff.staff_id)
    if conn is not None and (conn.blue_name != (me.get("fullName") or "") or conn.blue_email != (me.get("email") or "")):
        conn.blue_name, conn.blue_email = (me.get("fullName") or "")[:200], (me.get("email") or "")[:254]
        db.commit()
    return {"enabled": True, "connected": True, "name": me.get("fullName") or "", "email": me.get("email") or ""}


@router.get("/connect")
def connect(request: Request, _: Staff = Depends(ready_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    _enabled()
    redirect_uri = _redirect_uri(request)
    try:
        meta = blue.metadata(http)
        cid = blue.client_id(db, http, meta, redirect_uri)
    except blue.BlueError:
        log.warning("Blue connect failed", exc_info=True)
        return RedirectResponse("/?blue=failed", status_code=303)
    state = secrets.token_urlsafe(24)
    verifier, challenge = blue.new_pkce()
    resp = RedirectResponse(blue.authorize_url(meta, cid, redirect_uri, state, challenge), status_code=303)
    resp.set_cookie(PENDING_COOKIE, blue.pack_pending(state, verifier), max_age=600, httponly=True,
                    secure=get_settings().cookie_secure, samesite="lax", path="/api/blue")
    return resp


@router.get("/callback", name="blue_callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "",
             staff: Staff = Depends(ready_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    _enabled()
    pending = blue.unpack_pending(request.cookies.get(PENDING_COOKIE))
    result = "connected"
    if error:
        result = "cancelled"
    elif not pending or not state or not code or not secrets.compare_digest(pending[0], state):
        result = "failed"
    else:
        try:
            blue.connect(db, http, staff.staff_id, code, pending[1], _redirect_uri(request))
        except blue.BlueError:
            log.warning("Blue callback failed for %s", staff.staff_id, exc_info=True)
            result = "failed"
    resp = RedirectResponse(f"/?blue={result}", status_code=303)
    resp.delete_cookie(PENDING_COOKIE, path="/api/blue")
    return resp


@router.post("/disconnect")
def disconnect(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    _enabled()
    blue.disconnect(db, http, staff.staff_id)
    return {"ok": True}


# ---- boards (read records, comments, checklists; add comments; tick checklist items) ----
# Everything runs as the signed-in person with their own token, so Blue decides what they
# can see and do. IDs from the browser are only passed on to Blue, which checks them.

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RECONNECT = 409  # "connect Blue again" (not 401, which would log them out of this app)


def _id(value: str) -> str:
    if not ID.match(value or ""):
        raise HTTPException(400, "That doesn't look like a Blue ID.")
    return value


def _run(db: Session, http, staff: Staff, op: str, variables: dict | None = None, headers: dict | None = None,
         partial: bool = False) -> dict:
    _enabled()
    try:
        return blue.call(db, http, staff.staff_id, op, variables, headers, partial)
    except blue.BlueNotConnected:
        raise HTTPException(RECONNECT, "Connect Blue to see your boards.") from None
    except blue.BlueReconnect:
        raise HTTPException(RECONNECT, "Please connect Blue again.") from None
    except blue.BlueError as e:
        raise HTTPException(502, str(e)) from None


@router.get("/workspaces")
def workspaces(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    """Every workspace this person is a member of in Blue, grouped by organisation."""
    orgs = _run(db, http, staff, "Organizations")["organizations"]["items"]
    if not orgs:
        return {"organizations": []}
    items = _run(db, http, staff, "Workspaces", {"orgIds": [o["id"] for o in orgs]})["workspaceList"]["items"]
    # Grouped like Blue's sidebar: folders in Blue's order, then workspaces not in a folder.
    out = []
    for o in orgs:
        folders: dict[str, dict] = {}
        loose = {"id": "", "title": None, "color": None, "position": float("inf"), "workspaces": []}
        for w in items:
            if w["company"]["id"] != o["id"]:
                continue
            f = w.get("folder")
            group = loose if not f else folders.setdefault(f["id"], {
                "id": f["id"], "title": f["title"], "color": f.get("color"), "position": f.get("position") or 0, "workspaces": []})
            group["workspaces"].append({"id": w["id"], "name": w["name"], "color": w.get("color"), "icon": w.get("icon")})
        groups = sorted(folders.values(), key=lambda g: g["position"]) + ([loose] if loose["workspaces"] else [])
        for g in groups:
            g.pop("position")
        if groups:
            out.append({"id": o["id"], "name": o["name"], "groups": groups})
    return {"organizations": out}


MAX_COLUMNS = 40


def _card(r: dict) -> dict:
    return {
        "id": r["id"], "title": r["title"], "done": r["done"], "due": r.get("duedAt"), "start": r.get("startedAt"),
        "comments": r.get("commentCount", 0), "checklist": [r.get("checklistCompletedCount", 0), r.get("checklistCount", 0)],
        "tags": [{"title": t["title"], "color": t.get("color")} for t in r.get("tags") or []],
        "people": [u["fullName"] for u in r.get("users") or []],
    }


@router.get("/workspaces/{org_id}/{workspace_id}")
def board(org_id: str, workspace_id: str, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db),
          http=Depends(get_http)):
    """A workspace as a board: one column per list, in Blue's order, each column's records in
    Blue's top-to-bottom order. Blue only sorts by its manual order within a single list, so
    each list is read on its own; records outside every list go in a final "No list" column."""
    org_id, workspace_id = _id(org_id), _id(workspace_id)
    hdrs = {"blue-org-id": org_id, "blue-workspace-id": workspace_id}
    found = _run(db, http, staff, "Lists", {"workspaceId": workspace_id, "workspaceKey": workspace_id}, hdrs, partial=True)
    lists = [l for l in found.get("recordLists") or [] if not l.get("isDisabled")]
    databases = found.get("recordDatabases") or []
    lists.sort(key=lambda l: l.get("rank") or "")  # opaque keys, but they order lists within one workspace
    columns, truncated = [], len(lists) > MAX_COLUMNS
    lists = lists[:MAX_COLUMNS]
    try:  # every list at once, as this person
        results = blue.call_many(db, http, staff.staff_id, [
            ("ListRecords", {"orgId": org_id, "workspaceId": workspace_id, "listId": l["id"]}) for l in lists], hdrs)
    except blue.BlueReconnect:
        raise HTTPException(RECONNECT, "Please connect Blue again.") from None
    except blue.BlueError as e:
        raise HTTPException(502, str(e)) from None
    for l, res in zip(lists, results):
        todos = res["recordQueries"]["todos"]
        truncated = truncated or bool(todos["pageInfo"].get("hasNextPage"))
        columns.append({"id": l["id"], "title": (l["title"] or "").strip() or "Untitled", "records": [_card(r) for r in todos["items"]]})
    rest = _run(db, http, staff, "UnlistedRecords",
                {"orgId": org_id, "workspaceId": workspace_id, "notListIds": [l["id"] for l in lists]}, hdrs)["recordQueries"]["todos"]
    if rest["items"]:
        truncated = truncated or bool(rest["pageInfo"].get("hasNextPage"))
        columns.append({"id": "", "title": "No list" if lists else "Records", "records": [_card(r) for r in rest["items"]]})
    # Records are read through the workspace's main (first) database, as Blue's app does.
    return {"id": workspace_id, "database_id": databases[0]["id"] if databases else None,
            "columns": columns, "truncated": truncated}


def _comment_out(c: dict) -> dict:
    if "author" in c:  # recordComments shape
        a = c.get("author") or {}
        author = f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
    else:  # commentList / createComment shape
        author = (c.get("user") or {}).get("fullName") or ""
    return {"id": c["id"], "text": c.get("text") or "", "at": c["createdAt"], "author": author}


def _ws_headers(org_id: str, workspace_id: str) -> dict:
    """Blue needs the organisation and workspace for record reads and writes."""
    return {"blue-org-id": _id(org_id), "blue-workspace-id": _id(workspace_id)}


@router.get("/workspaces/{org_id}/{workspace_id}/records/{record_id}")
def record(org_id: str, workspace_id: str, record_id: str, db_id: str = "", staff: Staff = Depends(ready_staff),
           db: Session = Depends(get_db), http=Depends(get_http)):
    """One record: its fields, checklists and comments, exactly as Blue lets this person see them.
    If Blue won't share one part (say, the comments), the rest still shows."""
    hdrs, rid = _ws_headers(org_id, workspace_id), _id(record_id)
    database_id = _id(db_id) if db_id else None
    data = _run(db, http, staff, "Record", {"id": rid, "databaseId": database_id}, hdrs, partial=True)
    r = data.get("record")
    if not r:
        raise HTTPException(502, "Blue says you don't have permission to open this record.")
    missing = []
    if any((e.get("path") or [None, None])[1:2] == ["customFields"] for e in data.get("_errors", [])):
        missing.append("fields")
    if any((e.get("path") or [None, None])[1:2] == ["checklists"] for e in data.get("_errors", [])):
        missing.append("checklists")
    comments = None
    try:
        if database_id:
            res = _run(db, http, staff, "Comments", {"id": rid, "databaseId": database_id}, hdrs)
            entries = [c for c in (res.get("recordComments") or {}).get("entries") or [] if not c.get("deletedAt")]
        else:
            res = _run(db, http, staff, "LegacyComments", {"id": rid}, hdrs)
            entries = (res.get("commentList") or {}).get("comments") or []
        comments = sorted((_comment_out(c) for c in entries), key=lambda c: c["at"])
    except HTTPException as e:
        if e.status_code == RECONNECT:
            raise
        missing.append("comments")
    return {
        "id": r["id"], "title": r["title"], "description": r.get("text") or "", "done": r["done"],
        "due": r.get("duedAt"), "start": r.get("startedAt"), "list": (r.get("todoList") or {}).get("title"),
        "tags": [{"title": t["title"], "color": t.get("color")} for t in r.get("tags") or []],
        "people": [u["fullName"] for u in r.get("users") or []],
        # Blue's own per-person flag: a field this person may not view isn't shown.
        "fields": [{"id": f["id"], "name": f["name"], "type": f["type"], "value": f.get("value")}
                   for f in r.get("customFields") or [] if f.get("viewable") is not False],
        "can_tick": bool((r.get("checklistPermissions") or {}).get("canEditChecklistItem")),
        "checklists": [{"id": c["id"], "title": c["title"],
                        "items": [{"id": i["id"], "title": i["title"], "done": i["done"]} for i in c.get("checklistItems") or []]}
                       for c in sorted(r.get("checklists") or [], key=lambda c: c.get("position") or 0)],
        "comments": comments or [],
        "missing": missing,  # parts Blue wouldn't share with this person
    }


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


@router.post("/workspaces/{org_id}/{workspace_id}/records/{record_id}/comments")
def add_comment(org_id: str, workspace_id: str, record_id: str, body: CommentIn, staff: Staff = Depends(ready_staff),
                db: Session = Depends(get_db), http=Depends(get_http)):
    """Post a comment as this person (Blue records them as the author and sends its usual notifications)."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Write a comment first.")
    markup = "".join(f"<p>{html.escape(line)}</p>" for line in text.split("\n"))
    c = _run(db, http, staff, "AddComment", {"id": _id(record_id), "html": markup, "text": text},
             _ws_headers(org_id, workspace_id))["createComment"]
    return _comment_out(c)


class TickIn(BaseModel):
    done: bool


@router.post("/workspaces/{org_id}/{workspace_id}/checklist-items/{item_id}")
def tick(org_id: str, workspace_id: str, item_id: str, body: TickIn, staff: Staff = Depends(ready_staff),
         db: Session = Depends(get_db), http=Depends(get_http)):
    item = _run(db, http, staff, "TickChecklistItem", {"id": _id(item_id), "done": body.done},
                _ws_headers(org_id, workspace_id))["editChecklistItem"]
    return {"id": item["id"], "done": item["done"]}
