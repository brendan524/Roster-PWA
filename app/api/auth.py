import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import COOKIE_NAME, current_staff, ready_staff
from app.models import Staff
from app.security import (
    create_session,
    delete_all_sessions,
    delete_session,
    hash_password,
    now,
    validate_new_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

BAD_LOGIN = "Staff ID/email or password is incorrect."


class LoginIn(BaseModel):
    login: str  # Staff ID (e.g. AB0010) or email
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def me_payload(staff: Staff) -> dict:
    return {
        "staff_id": staff.staff_id,
        "name": staff.name,
        "role": staff.role,
        "department": staff.department,
        "must_change_password": staff.must_change_password,
        "has_leave": staff.has_leave,
        "email": staff.email,
    }


def _set_cookie(response: Response, token: str) -> None:
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


def authenticate(db: Session, login_id: str, password: str) -> Staff:
    """Check a Staff ID/email + password, with lockout. Raises HTTPException on failure."""
    s = get_settings()
    ident = login_id.strip()
    if "@" in ident:
        staff = db.query(Staff).filter_by(email=ident.lower()).first()
    else:
        staff = db.get(Staff, ident.upper()) if ident else None

    if staff is not None and staff.locked_until and staff.locked_until > now():
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")

    ok = verify_password(staff.password_hash if staff else None, password)
    if staff is None or not staff.active or not ok:
        if staff is not None and staff.active:
            staff.failed_logins += 1
            if staff.failed_logins >= s.max_failed_logins:
                staff.locked_until = now() + timedelta(minutes=s.lockout_minutes)
                staff.failed_logins = 0
            db.commit()
        raise HTTPException(401, BAD_LOGIN)

    staff.failed_logins = 0
    staff.locked_until = None
    db.commit()
    return staff


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    staff = authenticate(db, body.login, body.password)
    token = create_session(db, staff, request.headers.get("user-agent", ""))
    _set_cookie(response, token)
    return me_payload(staff)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    delete_session(db, request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(staff: Staff = Depends(current_staff)):
    return me_payload(staff)


@router.post("/change-password")
def change_password(
    body: ChangePasswordIn,
    request: Request,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_db),
):
    if not verify_password(staff.password_hash, body.current_password):
        raise HTTPException(400, "Current password is incorrect.")
    if body.new_password == body.current_password:
        raise HTTPException(400, "New password must be different.")
    err = validate_new_password(body.new_password)
    if err:
        raise HTTPException(400, err)
    staff.password_hash = hash_password(body.new_password)
    staff.must_change_password = False
    db.commit()
    # Sign out every other device that had the old password.
    delete_all_sessions(db, staff.staff_id, except_token=request.cookies.get(COOKIE_NAME))
    return me_payload(staff)


class EmailIn(BaseModel):
    email: str = Field("", max_length=254)
    password: str = Field(max_length=200)


@router.post("/email")
def set_email(body: EmailIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    """Staff set their own email (for notifications; it also works as their login).
    Needs their password, since it changes how they can log in."""
    if not verify_password(staff.password_hash, body.password):
        raise HTTPException(400, "Password is incorrect.")
    email = body.email.strip().lower() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "That email address doesn't look right.")
    if email and db.query(Staff).filter(Staff.email == email, Staff.staff_id != staff.staff_id).first():
        raise HTTPException(409, "That email is already used by someone else. Ask Brendan if it's yours.")
    staff.email = email
    db.commit()
    return me_payload(staff)
