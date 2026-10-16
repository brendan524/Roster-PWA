"""Staff list for admins, PIN/password resets (admin and self-service)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import admin_staff, ready_staff
from app.models import Staff
from app.security import delete_all_sessions, generate_password, generate_pin, hash_password, hash_pin, verify_password

router = APIRouter(prefix="/api", tags=["staff"])


def _new_pin(db: Session, staff: Staff) -> str:
    pin = generate_pin(db)  # random and unique: people never choose (that would reveal others' PINs)
    staff.pin_hash = hash_pin(pin)
    staff.pin_failed, staff.pin_locked_until = 0, None
    db.commit()
    return pin


class PasswordIn(BaseModel):
    password: str = Field(max_length=200)


@router.post("/auth/new-pin")
def my_new_pin(body: PasswordIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    if not verify_password(staff.password_hash, body.password):
        raise HTTPException(400, "Password is incorrect.")
    return {"pin": _new_pin(db, staff)}


@router.get("/admin/staff")
def list_staff(_: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    now = datetime.now(UTC)
    return {"staff": [{
        "staff_id": s.staff_id, "name": s.name, "role": s.role, "active": s.active, "email": s.email,
        "location_type": s.location_type, "employment_status": s.employment_status, "has_leave": s.has_leave,
        "has_pin": bool(s.pin_hash),
        "pin_locked": bool(s.pin_locked_until and s.pin_locked_until > now),
        "login_locked": bool(s.locked_until and s.locked_until > now),
        "must_change_password": s.must_change_password,
    } for s in sorted(db.query(Staff), key=lambda s: (not s.active, s.name.lower()))]}


def _get(db: Session, staff_id: str) -> Staff:
    s = db.get(Staff, staff_id)
    if s is None:
        raise HTTPException(404, "No such staff member.")
    return s


@router.post("/admin/staff/{staff_id}/reset-pin")
def admin_reset_pin(staff_id: str, _: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    s = _get(db, staff_id)
    return {"name": s.name, "pin": _new_pin(db, s)}


@router.post("/admin/staff/{staff_id}/reset-password")
def admin_reset_password(staff_id: str, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    s = _get(db, staff_id)
    if s.staff_id == admin.staff_id:
        raise HTTPException(400, "Change your own password under My details (tap your name).")
    pw = generate_password()
    s.password_hash = hash_password(pw)
    s.must_change_password = True
    s.failed_logins, s.locked_until = 0, None
    db.commit()
    delete_all_sessions(db, s.staff_id)
    return {"name": s.name, "password": pw}
