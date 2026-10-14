from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import COOKIE_NAME, current_staff
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

BAD_LOGIN = "Email or password is incorrect."


class LoginIn(BaseModel):
    email: str
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


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    s = get_settings()
    staff = db.query(Staff).filter_by(email=body.email.strip().lower()).first()

    if staff is not None and staff.locked_until and staff.locked_until > now():
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")

    ok = verify_password(staff.password_hash if staff else None, body.password)
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
