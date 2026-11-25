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


def _run(db: Session, http, staff: Staff, op: str, variables: dict | None = None, headers: dict | None = None) -> dict:
    _enabled()
    try:
        return blue.call(db, http, staff.staff_id, op, variables, headers)
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
    by_org: dict[str, list] = {}
    for w in items:
        by_org.setdefault(w["company"]["id"], []).append(
            {"id": w["id"], "name": w["name"], "color": w.get("color"), "icon": w.get("icon")})
    return {"organizations": [{"id": o["id"], "name": o["name"], "workspaces": by_org[o["id"]]}
                              for o in orgs if by_org.get(o["id"])]}


@router.get("/workspaces/{org_id}/{workspace_id}")
def board(org_id: str, workspace_id: str, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db),
          http=Depends(get_http)):
    """A workspace's records, grouped into its lists (board columns)."""
    org_id, workspace_id = _id(org_id), _id(workspace_id)
    data = _run(db, http, staff, "Board", {"orgId": org_id, "workspaceId": workspace_id},
                headers={"blue-org-id": org_id, "blue-workspace-id": workspace_id})
    ws, todos = data["workspace"], data["recordQueries"]["todos"]
    columns = [{"id": l["id"], "title": l["title"], "records": []} for l in ws.get("todoLists") or []]
    col = {c["id"]: c for c in columns}
    other = {"id": "", "title": "No list", "records": []}
    # Blue only sorts by its manual "rank" within one list, so the query spans the workspace
    # in creation order and each column is put in board order here: rank is an opaque string
    # compared only within its own list, highest first (Blue's top-to-bottom order).
    items = sorted(todos["items"], key=lambda r: r.get("rank") or "", reverse=True)
    for r in items:
        (col.get((r.get("todoList") or {}).get("id")) or other)["records"].append({
            "id": r["id"], "title": r["title"], "done": r["done"], "due": r.get("duedAt"), "start": r.get("startedAt"),
            "comments": r.get("commentCount", 0), "checklist": [r.get("checklistCompletedCount", 0), r.get("checklistCount", 0)],
            "tags": [{"title": t["title"], "color": t.get("color")} for t in r.get("tags") or []],
            "people": [u["fullName"] for u in r.get("users") or []],
        })
    if other["records"]:
        columns.append(other)
    return {"id": ws["id"], "name": ws["name"], "color": ws.get("color"), "columns": columns,
            "truncated": bool(todos["pageInfo"].get("hasNextPage"))}


def _comment_out(c: dict) -> dict:
    return {"id": c["id"], "text": c.get("text") or "", "at": c["createdAt"],
            "author": (c.get("user") or {}).get("fullName") or ""}


@router.get("/records/{record_id}")
def record(record_id: str, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    """One record: its fields, checklists and comments, exactly as Blue lets this person see them."""
    data = _run(db, http, staff, "Record", {"id": _id(record_id)})
    r = data["record"]
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
        "comments": [_comment_out(c) for c in (data.get("commentList") or {}).get("comments") or []],
    }


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


@router.post("/records/{record_id}/comments")
def add_comment(record_id: str, body: CommentIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db),
                http=Depends(get_http)):
    """Post a comment as this person (Blue records them as the author and sends its usual notifications)."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Write a comment first.")
    markup = "".join(f"<p>{html.escape(line)}</p>" for line in text.split("\n"))
    c = _run(db, http, staff, "AddComment", {"id": _id(record_id), "html": markup, "text": text})["createComment"]
    return _comment_out(c)


class TickIn(BaseModel):
    done: bool


@router.post("/checklist-items/{item_id}")
def tick(item_id: str, body: TickIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db),
         http=Depends(get_http)):
    item = _run(db, http, staff, "TickChecklistItem", {"id": _id(item_id), "done": body.done})["editChecklistItem"]
    return {"id": item["id"], "done": item["done"]}
