from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Staff(Base):
    """A staff member. The database is the source of truth for staff after seeding."""

    __tablename__ = "staff"

    # Roster join key, e.g. "AB0001". Always stored upper-case.
    staff_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True)  # stored lower-case
    department: Mapped[str] = mapped_column(String(60), default="")
    location_type: Mapped[str] = mapped_column(String(10))  # "onshore" | "offshore"
    join_date: Mapped[date | None] = mapped_column(Date)
    role: Mapped[str] = mapped_column(String(10), default="staff")  # "staff" | "admin"
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    password_hash: Mapped[str | None] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    # HMAC-SHA256 of the PIN with a server-side pepper. Unique so a PIN maps to exactly one person.
    pin_hash: Mapped[str | None] = mapped_column(String(64), unique=True)

    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthSession(Base):
    """A logged-in device. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    staff_id: Mapped[str] = mapped_column(
        ForeignKey("staff.staff_id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str] = mapped_column(String(255), default="")
