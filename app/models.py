from datetime import date, datetime, time

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, Numeric, String, Time, func
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
    has_leave: Mapped[bool] = mapped_column(Boolean, default=True)  # False for contractors: no leave in the app
    auto_clock: Mapped[bool] = mapped_column(Boolean, default=False)  # clocked in/out at rostered times automatically
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
    source: Mapped[str] = mapped_column(String(10))  # "personal" | "kiosk" | "admin" | "auto"
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


class LeaveRequest(Base):
    """A leave application. Status moves pending → approved/declined, or pending → cancelled."""

    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.staff_id"), index=True)
    leave_type: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    start_half: Mapped[bool] = mapped_column(Boolean, default=False)  # first day is a half day
    end_half: Mapped[bool] = mapped_column(Boolean, default=False)  # last day is a half day
    days: Mapped[float] = mapped_column(Numeric(5, 1))  # working days, counted when submitted
    reason: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(10), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("staff.staff_id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(String(500), default="")


class LeaveAdjustment(Base):
    """Manual balance change by an admin (append-only), e.g. an opening balance at go-live.

    kind "set": the balance becomes `amount` on `effective_date` (earlier history no longer counts).
    kind "add": `amount` (+ or -) is added on `effective_date`.
    """

    __tablename__ = "leave_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.staff_id"), index=True)
    leave_type: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(4))  # set | add
    amount: Mapped[float] = mapped_column(Numeric(6, 2))
    effective_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(300))
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class XeroConnection(Base):
    """The single connection to AB Group's Xero organisation. Tokens are stored encrypted."""

    __tablename__ = "xero_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    tenant_id: Mapped[str] = mapped_column(String(64))
    tenant_name: Mapped[str] = mapped_column(String(200), default="")
    access_token_enc: Mapped[str] = mapped_column(String(8000))
    refresh_token_enc: Mapped[str] = mapped_column(String(2000))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    connected_by: Mapped[str] = mapped_column(String(32))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(1000), default="")


class XeroEmployeeLink(Base):
    """Which Xero payroll employee a staff member is."""

    __tablename__ = "xero_employee_links"

    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.staff_id", ondelete="CASCADE"), primary_key=True)
    xero_employee_id: Mapped[str] = mapped_column(String(64), unique=True)
    xero_name: Mapped[str] = mapped_column(String(200), default="")
    linked_by: Mapped[str] = mapped_column(String(32))  # "auto" or an admin's staff id
    start_date: Mapped[date | None] = mapped_column(Date)  # employment start in Xero (long service)
    employment_basis: Mapped[str] = mapped_column(String(20), default="")  # FULLTIME / PARTTIME / CASUAL ...


class XeroLeaveBalance(Base):
    """Leave balance copied from Xero at the last sync (Xero stays the source of truth)."""

    __tablename__ = "xero_leave_balances"

    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.staff_id", ondelete="CASCADE"), primary_key=True)
    leave_type_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    leave_name: Mapped[str] = mapped_column(String(200))
    units: Mapped[float] = mapped_column(Numeric(10, 4))
    unit_type: Mapped[str] = mapped_column(String(20), default="Hours")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LeaveAttachment(Base):
    """A file on a leave request (e.g. a medical certificate). Stored in the database so backups include it."""

    __tablename__ = "leave_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("leave_requests.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(60))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    uploaded_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AvailabilityWeekly(Base):
    """A regular weekly time someone can't work. No start/end = the whole day."""

    __tablename__ = "availability_weekly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.staff_id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday ... 6 = Sunday
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AvailabilityDate(Base):
    """One-off dates someone can't work (each day in the range; optionally only between two times)."""

    __tablename__ = "availability_dates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff.staff_id", ondelete="CASCADE"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailOutbox(Base):
    """Emails waiting to go (or sent). Sent in the background with retries."""

    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    to_addr: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(String)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)  # stops repeats of the same alert/digest
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(500), default="")


class AppState(Base):
    """Small key/value notes the background jobs keep (e.g. when the last off-site backup arrived)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
