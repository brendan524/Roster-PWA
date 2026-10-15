from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Staff(Base):
    """A staff member. The database is the source of truth for staff after seeding."""

    __tablename__ = "staff"

    # Roster join key, e.g. "AB0001". Always stored upper-case.
    staff_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(254), unique=True)  # optional; stored lower-case
    department: Mapped[str] = mapped_column(String(60), default="")
    location_type: Mapped[str] = mapped_column(String(10))  # "onshore" | "offshore"
    join_date: Mapped[date | None] = mapped_column(Date)
    employment_status: Mapped[str] = mapped_column(String(40), default="")  # Director / Casual / Contract...
    notes: Mapped[str] = mapped_column(String(500), default="")
    role: Mapped[str] = mapped_column(String(10), default="staff")  # "staff" | "admin"
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    password_hash: Mapped[str | None] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    # HMAC-SHA256 of the PIN with a server-side pepper. Unique so a PIN maps to exactly one person.
    pin_hash: Mapped[str | None] = mapped_column(String(64), unique=True)

    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Wrong-PIN protection for clocking on a personal device.
    pin_failed: Mapped[int] = mapped_column(Integer, default=0)
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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


class Kiosk(Base):
    """A shared clock-in device at a fixed office. Authenticates with a device token."""

    __tablename__ = "kiosks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60))  # e.g. "AB HQ"
    location_type: Mapped[str] = mapped_column(String(10))  # onshore | offshore (decides the clock shown)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("staff.staff_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Wrong-PIN protection: the whole kiosk pauses briefly after repeated misses.
    pin_failed: Mapped[int] = mapped_column(Integer, default=0)
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Punch(Base):
    """One clock-in or clock-out. Append-only: never updated or deleted.

    Corrections are new rows (source="admin") plus PunchVoid rows, so the
    original record and who changed what are always kept.
    """

    __tablename__ = "punches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Generated on the device; makes re-sending a queued punch safe (idempotency key).
    client_id: Mapped[str] = mapped_column(String(64), unique=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.staff_id"), index=True)
    kind: Mapped[str] = mapped_column(String(3))  # "in" | "out"
    source: Mapped[str] = mapped_column(String(10))  # "personal" | "kiosk" | "admin"
    kiosk_id: Mapped[int | None] = mapped_column(ForeignKey("kiosks.id"))

    # The moment the punch happened (UTC). Server time when sent live; for
    # punches queued offline, the device time corrected by the measured clock offset.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    device_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clock_offset_s: Mapped[int | None] = mapped_column(Integer)  # server minus device clock
    queued: Mapped[bool] = mapped_column(Boolean, default=False)  # sent later from an offline queue

    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    geo_status: Mapped[str] = mapped_column(String(12))  # ok | denied | unavailable | timeout | kiosk | admin

    created_by: Mapped[str | None] = mapped_column(ForeignKey("staff.staff_id"))  # admin entries
    reason: Mapped[str] = mapped_column(String(300), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class PunchVoid(Base):
    """Marks a punch as not counting (e.g. entered by mistake). The punch row itself is kept."""

    __tablename__ = "punch_voids"

    punch_id: Mapped[int] = mapped_column(ForeignKey("punches.id"), primary_key=True)
    voided_by: Mapped[str] = mapped_column(ForeignKey("staff.staff_id"))
    reason: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
