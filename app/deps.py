from functools import lru_cache

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Staff
from app.roster import CsvDirectorySource, GoogleSheetsSource, RosterService
from app.security import renew_session, session_for_token

COOKIE_NAME = "ab_session"


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=s.session_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )


def current_staff(request: Request, response: Response, db: Session = Depends(get_db)) -> Staff:
    token = request.cookies.get(COOKIE_NAME)
    found = session_for_token(db, token)
    if found is None:
        raise HTTPException(401, "Please log in.")
    sess, staff = found
    if renew_session(db, sess):
        # Fresh cookie too, so the browser doesn't drop it at the old expiry.
        set_session_cookie(response, token)
    return staff


def ready_staff(staff: Staff = Depends(current_staff)) -> Staff:
    """A logged-in staff member who has replaced their temporary password."""
    if staff.must_change_password:
        raise HTTPException(403, "Please set a new password first.")
    return staff


def admin_staff(staff: Staff = Depends(ready_staff)) -> Staff:
    if not staff.is_admin:
        raise HTTPException(403, "Admins only.")
    return staff


@lru_cache
def get_roster_service() -> RosterService:
    s = get_settings()
    if s.roster_source == "csv":
        source = CsvDirectorySource(s.roster_csv_dir)
    else:
        source = GoogleSheetsSource(s.roster_sheet_id, s.google_service_account_file)
    return RosterService(source, s.timezone, s.roster_cache_seconds, s.roster_min_refresh_seconds)
