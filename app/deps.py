from functools import lru_cache

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Staff
from app.roster import CsvDirectorySource, GoogleSheetsSource, RosterService
from app.security import staff_for_token

COOKIE_NAME = "ab_session"


def current_staff(request: Request, db: Session = Depends(get_db)) -> Staff:
    staff = staff_for_token(db, request.cookies.get(COOKIE_NAME))
    if staff is None:
        raise HTTPException(401, "Please log in.")
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
