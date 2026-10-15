"""Read onshore leave balances from Xero Payroll (Australia). Read-only: Xero stays the source of truth.

Flow: an admin taps "Connect Xero" once and approves access in Xero. The app keeps
an (encrypted) refresh token and syncs every few hours: it lists payroll employees,
matches them to staff (exact name or email; admins can fix matches), and copies
each matched person's leave balances.

Xero refresh tokens rotate on every use and expire if unused for 60 days, so the
periodic sync also keeps the connection alive.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Staff, XeroConnection, XeroEmployeeLink, XeroLeaveBalance

log = logging.getLogger(__name__)

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REVOKE_URL = "https://identity.xero.com/connect/revocation"
CONNECTIONS_URL = "https://api.xero.com/connections"
PAYROLL = "https://api.xero.com/payroll.xro/1.0"
SCOPES = "offline_access payroll.employees.read payroll.settings.read"
TIMEOUT = 30

# Employees seen at the last sync, for the admin "link staff" picker.
_last_employees: list[dict] = []


class XeroError(RuntimeError):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s.xero_client_id and s.xero_client_secret)


# ---- token encryption (key derived from the server secret, never stored in the DB) ----

def _fernet() -> Fernet:
    pepper = get_settings().pin_pepper
    if not pepper:
        raise XeroError("PIN_PEPPER is not set")
    key = hmac.new(pepper.encode(), b"xero-token-encryption", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enc(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _dec(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise XeroError("Stored Xero tokens can't be read (server secret changed?). Reconnect Xero.") from e


# ---- OAuth ------------------------------------------------------------------------------

def authorize_url(state: str, redirect_uri: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": get_settings().xero_client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })


def _token_request(http, data: dict) -> dict:
    s = get_settings()
    r = http.post(TOKEN_URL, data=data, auth=(s.xero_client_id, s.xero_client_secret), timeout=TIMEOUT)
    if r.status_code != 200:
        raise XeroError(f"Xero sign-in failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def connect(db: Session, http, code: str, redirect_uri: str, admin_id: str) -> XeroConnection:
    tokens = _token_request(http, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    r = http.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise XeroError(f"Couldn't list Xero organisations ({r.status_code})")
    orgs = [c for c in r.json() if c.get("tenantType") == "ORGANISATION"]
    if not orgs:
        raise XeroError("No Xero organisation was shared with the app.")
    org = orgs[0]
    conn = db.get(XeroConnection, 1) or XeroConnection(id=1)
    conn.tenant_id = org["tenantId"]
    conn.tenant_name = org.get("tenantName", "")
    conn.access_token_enc = _enc(tokens["access_token"])
    conn.refresh_token_enc = _enc(tokens["refresh_token"])
    conn.expires_at = datetime.now(UTC) + timedelta(seconds=int(tokens.get("expires_in", 1800)))
    conn.connected_by = admin_id
    conn.connected_at = datetime.now(UTC)
    conn.last_error = ""
    db.add(conn)
    db.commit()
    return conn


def disconnect(db: Session, http) -> None:
    conn = db.get(XeroConnection, 1)
    if conn is None:
        return
    try:
        s = get_settings()
        http.post(REVOKE_URL, data={"token": _dec(conn.refresh_token_enc)},
                  auth=(s.xero_client_id, s.xero_client_secret), timeout=TIMEOUT)
    except Exception:  # noqa: BLE001 — still forget it locally
        log.warning("Xero token revoke failed", exc_info=True)
    db.delete(conn)
    db.query(XeroLeaveBalance).delete()
    db.commit()


def _access_token(db: Session, http, force_refresh: bool = False) -> tuple[str, str]:
    """A valid access token and tenant id, refreshing (and rotating the refresh token) if needed."""
    conn = db.query(XeroConnection).filter_by(id=1).with_for_update().first()
    if conn is None:
        db.rollback()
        raise XeroError("Xero isn't connected.")
    if not force_refresh and conn.expires_at > datetime.now(UTC) + timedelta(seconds=60):
        token, tenant = _dec(conn.access_token_enc), conn.tenant_id
        db.commit()
        return token, tenant
    try:
        tokens = _token_request(http, {"grant_type": "refresh_token", "refresh_token": _dec(conn.refresh_token_enc)})
    except XeroError as e:
        msg = "The Xero connection has expired or was removed. An admin needs to reconnect Xero (Leave tab)."
        conn.last_error = f"{msg} ({e})"[:1000]
        db.commit()
        raise XeroError(msg) from e
    conn.access_token_enc = _enc(tokens["access_token"])
    conn.refresh_token_enc = _enc(tokens["refresh_token"])
    conn.expires_at = datetime.now(UTC) + timedelta(seconds=int(tokens.get("expires_in", 1800)))
    tenant = conn.tenant_id
    db.commit()
    return tokens["access_token"], tenant


def api_get(db: Session, http, path: str, params: dict | None = None) -> dict:
    for attempt in (0, 1):
        token, tenant = _access_token(db, http, force_refresh=attempt == 1)
        r = http.get(f"{PAYROLL}/{path}", params=params or {}, timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {token}", "Xero-tenant-id": tenant, "Accept": "application/json"})
        if r.status_code == 401 and attempt == 0:
            continue  # token rejected early: refresh once and retry
        if r.status_code == 429:
            raise XeroError("Xero rate limit reached — try again in a minute.")
        if r.status_code != 200:
            raise XeroError(f"Xero request failed ({r.status_code}): {r.text[:200]}")
        return r.json()
    raise XeroError("Xero rejected the app's access. Reconnect Xero.")


# ---- sync ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def fetch_employees(db: Session, http) -> list[dict]:
    out, page = [], 1
    while True:
        batch = api_get(db, http, "Employees", {"page": page}).get("Employees", [])
        for e in batch:
            out.append({"id": e["EmployeeID"], "name": f"{e.get('FirstName', '')} {e.get('LastName', '')}".strip(),
                        "email": (e.get("Email") or "").lower(), "status": e.get("Status", "")})
        if len(batch) < 100:
            return out
        page += 1


def auto_link(db: Session, employees: list[dict]) -> int:
    """Link unlinked onshore staff to Xero employees by exact full name (or email). Returns how many."""
    linked_ids = {l.xero_employee_id for l in db.query(XeroEmployeeLink)}
    linked_staff = {l.staff_id for l in db.query(XeroEmployeeLink)}
    by_name: dict[str, list[dict]] = {}
    for e in employees:
        if e["id"] not in linked_ids:
            by_name.setdefault(_norm(e["name"]), []).append(e)
    n = 0
    for s in db.query(Staff).filter_by(location_type="onshore", active=True):
        if s.staff_id in linked_staff:
            continue
        match = None
        if s.email:
            match = next((e for e in employees if e["email"] == s.email and e["id"] not in linked_ids), None)
        if match is None:
            candidates = by_name.get(_norm(s.name), [])
            match = candidates[0] if len(candidates) == 1 else None  # ambiguous names need a person to decide
        if match:
            db.add(XeroEmployeeLink(staff_id=s.staff_id, xero_employee_id=match["id"], xero_name=match["name"],
                                    linked_by="auto"))
            linked_ids.add(match["id"])
            n += 1
    db.commit()
    return n


def sync(db: Session, http=None) -> dict:
    """Refresh everyone's leave balances from Xero. Records the outcome on the connection."""
    global _last_employees
    http = http or requests.Session()
    try:
        employees = fetch_employees(db, http)
        _last_employees = sorted(employees, key=lambda e: e["name"].lower())
        auto = auto_link(db, employees)
        now = datetime.now(UTC)
        updated = 0
        links = db.query(XeroEmployeeLink).all()
        for link in links:
            data = api_get(db, http, f"Employees/{link.xero_employee_id}")
            emp = (data.get("Employees") or [{}])[0]
            db.query(XeroLeaveBalance).filter_by(staff_id=link.staff_id).delete()
            for lb in emp.get("LeaveBalances") or []:
                db.add(XeroLeaveBalance(staff_id=link.staff_id, leave_type_id=lb["LeaveTypeID"],
                                        leave_name=lb.get("LeaveName", "Leave"),
                                        units=Decimal(str(lb.get("NumberOfUnits") or 0)),
                                        unit_type=lb.get("TypeOfUnits") or "Hours", fetched_at=now))
            db.commit()
            updated += 1
        conn = db.get(XeroConnection, 1)
        conn.last_sync_at, conn.last_error = now, ""
        db.commit()
        return {"employees": len(employees), "auto_linked": auto, "updated": updated}
    except XeroError as e:
        db.rollback()
        conn = db.get(XeroConnection, 1)
        if conn is not None:
            conn.last_error = str(e)[:1000]
            db.commit()
        raise


def last_employees() -> list[dict]:
    return _last_employees


# ---- background sync ----------------------------------------------------------------

def start_background_sync(session_factory, hours: float) -> None:
    """Sync shortly after start-up and then every `hours`, if Xero is set up and connected."""
    if hours <= 0 or not configured():
        return

    def loop():
        time.sleep(30)
        while True:
            try:
                with session_factory() as db:
                    if db.get(XeroConnection, 1) is not None:
                        result = sync(db)
                        log.info("Xero sync: %s", result)
            except Exception:  # noqa: BLE001 — keep the loop alive; error is stored on the connection
                log.exception("Xero sync failed")
            time.sleep(hours * 3600)

    threading.Thread(target=loop, name="xero-sync", daemon=True).start()
