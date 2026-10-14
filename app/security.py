"""Password, PIN and session-token helpers."""

import hashlib
import hmac
import secrets
import string
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuthSession, Staff

_hasher = PasswordHasher()
# Verified against when the email is unknown, so response time doesn't reveal which emails exist.
_DUMMY_HASH = _hasher.hash("not-a-real-password")

MIN_PASSWORD_LENGTH = 10


def now() -> datetime:
    return datetime.now(UTC)


# ---- passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def validate_new_password(password: str) -> str | None:
    """Return an error message, or None if the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.isdigit():
        return "Password can't be only numbers."
    return None


def generate_password() -> str:
    # Readable temporary password: no ambiguous characters.
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


# ---- PINs ------------------------------------------------------------------
# PINs are short, so a slow salted hash adds little against brute force if the
# database leaked. Instead we use HMAC with a secret pepper kept OUT of the
# database. This is deterministic, so a kiosk can resolve PIN -> staff member
# with one indexed lookup, and the unique index guarantees no two staff share a PIN.

def hash_pin(pin: str) -> str:
    pepper = get_settings().pin_pepper
    if not pepper:
        raise RuntimeError("PIN_PEPPER is not set")
    return hmac.new(pepper.encode(), pin.encode(), hashlib.sha256).hexdigest()


def validate_pin(pin: str) -> str | None:
    length = get_settings().pin_length
    if not (pin.isdigit() and len(pin) == length):
        return f"PIN must be exactly {length} digits."
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "PIN is too easy to guess."
    return None


def generate_pin(db: Session) -> str:
    length = get_settings().pin_length
    while True:
        pin = "".join(secrets.choice(string.digits) for _ in range(length))
        if validate_pin(pin) is None and not db.query(Staff).filter_by(pin_hash=hash_pin(pin)).first():
            return pin


# ---- sessions --------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, staff: Staff, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token_hash=_token_hash(token),
            staff_id=staff.staff_id,
            expires_at=now() + timedelta(days=get_settings().session_days),
            user_agent=user_agent[:255],
        )
    )
    db.commit()
    return token


def staff_for_token(db: Session, token: str | None) -> Staff | None:
    if not token:
        return None
    sess = db.get(AuthSession, _token_hash(token))
    if sess is None or sess.expires_at <= now():
        return None
    staff = db.get(Staff, sess.staff_id)
    if staff is None or not staff.active:
        return None
    return staff


def delete_session(db: Session, token: str | None) -> None:
    if token:
        db.query(AuthSession).filter_by(token_hash=_token_hash(token)).delete()
        db.commit()


def delete_all_sessions(db: Session, staff_id: str, except_token: str | None = None) -> None:
    q = db.query(AuthSession).filter_by(staff_id=staff_id)
    if except_token:
        q = q.filter(AuthSession.token_hash != _token_hash(except_token))
    q.delete()
    db.commit()
