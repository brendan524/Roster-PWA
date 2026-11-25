"""Each staff member's own Blue connection: connect, status, disconnect.

No Blue token or secret is ever sent to the browser; it only learns whether the person is
connected and the name on their Blue account.
"""

import logging
import secrets

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
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
