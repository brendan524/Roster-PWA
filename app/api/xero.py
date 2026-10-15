"""Admin endpoints for the Xero connection."""

import secrets

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import xero
from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff
from app.models import Staff, XeroConnection, XeroEmployeeLink, XeroLeaveBalance

router = APIRouter(prefix="/api/admin/xero", tags=["xero"])

STATE_COOKIE = "ab_xero_state"


def get_http():
    """Overridden in tests with a fake Xero."""
    return requests.Session()


def _redirect_uri(request: Request) -> str:
    return get_settings().xero_redirect_uri or str(request.url_for("xero_callback"))


def balances_out(rows: list[XeroLeaveBalance]) -> list[dict]:
    return [{"name": b.leave_name, "units": float(b.units), "unit_type": b.unit_type,
             "fetched_at": b.fetched_at.isoformat()} for b in sorted(rows, key=lambda b: b.leave_name)]


@router.get("/status")
def status(_: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    conn = db.get(XeroConnection, 1)
    links = {l.staff_id: l for l in db.query(XeroEmployeeLink)}
    bal: dict[str, list] = {}
    for b in db.query(XeroLeaveBalance):
        bal.setdefault(b.staff_id, []).append(b)
    onshore = sorted(db.query(Staff).filter_by(location_type="onshore", active=True), key=lambda s: s.name)
    return {
        "configured": xero.configured(),
        "connected": conn is not None,
        "tenant_name": conn.tenant_name if conn else None,
        "last_sync_at": conn.last_sync_at.isoformat() if conn and conn.last_sync_at else None,
        "last_error": conn.last_error if conn else None,
        "staff": [{
            "staff_id": s.staff_id, "name": s.name,
            "link": ({"xero_employee_id": links[s.staff_id].xero_employee_id, "xero_name": links[s.staff_id].xero_name,
                      "linked_by": links[s.staff_id].linked_by} if s.staff_id in links else None),
            "balances": balances_out(bal.get(s.staff_id, [])),
        } for s in onshore],
        "xero_employees": [{"id": e["id"], "name": e["name"]} for e in xero.last_employees()],
    }


@router.get("/connect")
def connect(request: Request, _: Staff = Depends(admin_staff)):
    if not xero.configured():
        raise HTTPException(400, "Xero isn't set up on the server yet (XERO_CLIENT_ID / XERO_CLIENT_SECRET).")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(xero.authorize_url(state, _redirect_uri(request)), status_code=303)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=get_settings().cookie_secure,
                    samesite="lax", path="/api/admin/xero")
    return resp


@router.get("/callback", name="xero_callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "",
             admin: Staff = Depends(admin_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    expected = request.cookies.get(STATE_COOKIE)
    result = "connected"
    if error:
        result = "cancelled"
    elif not expected or not state or not secrets.compare_digest(expected, state) or not code:
        result = "failed"
    else:
        try:
            xero.connect(db, http, code, _redirect_uri(request), admin.staff_id)
            try:
                xero.sync(db, http)
            except xero.XeroError:
                pass  # connected; the sync error is shown on the Leave tab
        except xero.XeroError:
            result = "failed"
    resp = RedirectResponse(f"/?xero={result}", status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/api/admin/xero")
    return resp


@router.post("/sync")
def sync_now(_: Staff = Depends(admin_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    try:
        return xero.sync(db, http)
    except xero.XeroError as e:
        raise HTTPException(502, str(e)) from e


class LinkIn(BaseModel):
    staff_id: str
    xero_employee_id: str | None = None  # None = unlink


@router.post("/link")
def link(body: LinkIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    staff = db.get(Staff, body.staff_id)
    if staff is None:
        raise HTTPException(404, "No such staff member.")
    db.query(XeroEmployeeLink).filter_by(staff_id=staff.staff_id).delete()
    db.query(XeroLeaveBalance).filter_by(staff_id=staff.staff_id).delete()
    if body.xero_employee_id:
        emp = next((e for e in xero.last_employees() if e["id"] == body.xero_employee_id), None)
        if emp is None:
            raise HTTPException(400, "Unknown Xero employee — tap Sync first.")
        previous = db.query(XeroEmployeeLink).filter_by(xero_employee_id=emp["id"]).first()
        if previous is not None:  # move it from whoever had it, with their copied balances
            db.query(XeroLeaveBalance).filter_by(staff_id=previous.staff_id).delete()
            db.delete(previous)
            db.flush()
        db.add(XeroEmployeeLink(staff_id=staff.staff_id, xero_employee_id=emp["id"], xero_name=emp["name"],
                                linked_by=admin.staff_id))
    db.commit()
    return {"ok": True}


@router.post("/disconnect")
def disconnect(_: Staff = Depends(admin_staff), db: Session = Depends(get_db), http=Depends(get_http)):
    xero.disconnect(db, http)
    return {"ok": True}
