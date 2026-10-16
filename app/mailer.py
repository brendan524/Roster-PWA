"""Outgoing email. Messages are queued in the database and sent by a background
job with retries, so a slow or unreachable mail server never holds up the app
and nothing is lost if it's down for a while."""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmailOutbox, Staff

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
FOOTER = "\n\n—\nAB Group staff app{url}\nThis is an automatic email."


def configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_user and s.smtp_password)


def _footer() -> str:
    url = get_settings().app_url
    return FOOTER.format(url=f" · {url}" if url else "")


def queue(db: Session, to: str | None, subject: str, body: str, dedupe_key: str | None = None) -> bool:
    """Add an email to the outbox (in the caller's transaction). Returns False if skipped:
    no address, email not set up, or the same dedupe_key was already queued."""
    if not to or not configured():
        return False
    stmt = insert(EmailOutbox).values(to_addr=to, subject=subject[:300], body=body + _footer(),
                                      dedupe_key=dedupe_key[:200] if dedupe_key else None)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    # RETURNING tells us whether a row went in (rowcount isn't reliable for single-row inserts here).
    return db.execute(stmt.returning(EmailOutbox.id)).first() is not None


def queue_admins(db: Session, subject: str, body: str, dedupe_key: str | None = None,
                 exclude: str | None = None) -> int:
    n = 0
    for a in db.query(Staff).filter_by(role="admin", active=True):
        if a.email and a.staff_id != exclude:
            n += queue(db, a.email, subject, body, f"{dedupe_key}:{a.staff_id}" if dedupe_key else None)
    return n


def alert(db: Session, subject: str, body: str, dedupe_key: str) -> bool:
    """A system alert to ALERT_EMAIL, at most once per dedupe_key."""
    return queue(db, get_settings().alert_email, f"[AB app] {subject}", body, f"alert:{dedupe_key}")


def _message(to: str, subject: str, body: str) -> EmailMessage:
    s = get_settings()
    sender = s.mail_from or s.smtp_user
    msg = EmailMessage()
    msg["From"] = formataddr(("AB Group", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(body)
    return msg


def _connect() -> smtplib.SMTP:
    s = get_settings()
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        conn = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=ctx)
    else:
        conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        conn.starttls(context=ctx)
    conn.login(s.smtp_user, s.smtp_password)
    return conn


def send_direct(to: str, subject: str, body: str) -> None:
    """Send right now, bypassing the database (used for alerts when the database may be the problem)."""
    with _connect() as conn:
        conn.send_message(_message(to, subject, body + _footer()))


def send_pending(db: Session, now: datetime | None = None, connect=_connect) -> int:
    """Send what's due. Returns how many were sent."""
    if not configured():
        return 0
    now = now or datetime.now(UTC)
    due = (db.query(EmailOutbox)
           .filter(EmailOutbox.sent_at.is_(None), EmailOutbox.attempts < MAX_ATTEMPTS,
                   EmailOutbox.next_attempt_at <= now)
           .order_by(EmailOutbox.id).limit(50).with_for_update(skip_locked=True).all())
    if not due:
        db.commit()
        return 0
    sent = 0
    try:
        conn = connect()
    except Exception as e:  # noqa: BLE001 — server down, wrong password...
        for m in due:
            _failed(m, now, e)
        db.commit()
        log.warning("Email: couldn't connect to the mail server: %s", e)
        return 0
    try:
        for m in due:
            try:
                conn.send_message(_message(m.to_addr, m.subject, m.body))
                m.sent_at, m.last_error = now, ""
                m.attempts += 1
                sent += 1
            except Exception as e:  # noqa: BLE001
                _failed(m, now, e)
            db.commit()
    finally:
        try:
            conn.quit()
        except Exception:  # noqa: BLE001
            pass
    return sent


def _failed(m: EmailOutbox, now: datetime, e: Exception) -> None:
    m.attempts += 1
    m.last_error = f"{type(e).__name__}: {e}"[:500]
    m.next_attempt_at = now + timedelta(minutes=2 ** m.attempts)  # 2, 4, 8 ... ~4 hours
