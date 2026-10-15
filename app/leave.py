"""Leave policy and balances.

Offshore policy (from the brief, confirmed with Brendan):
- 6-month probation from the join date. Credits start at the START of month 7,
  i.e. on the 6-month anniversary, then on the same day each month after.
- Vacation: +1 day per month, carried forward, balance capped at 10 days
  (a credit that would go over 10 is only partly given, or not at all).
- Sick: +0.5 day per month, carried forward, no cap.

Balances are not stored; they're recalculated from the join date, approved
leave and admin adjustments every time. That keeps them consistent if a
request is approved late or an adjustment is back-dated, and "the scheduled
accrual job" can never miss a month or run twice.

Onshore balances live in Xero (Stage 3b); the app records their applications only.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import LeaveAdjustment, LeaveRequest, Staff

PROBATION_MONTHS = 6
MAX_REQUEST_DAYS = 92  # longest date range one request can cover


@dataclass(frozen=True)
class LeaveType:
    code: str
    label: str
    accrual: Decimal | None = None  # days per month; None = no balance kept by this app
    cap: Decimal | None = None
    advance_only: bool = True  # must start after today; False = can also be for past dates (e.g. sick)


OFFSHORE_TYPES = [
    LeaveType("vacation", "Vacation leave", Decimal("1"), Decimal("10")),
    LeaveType("sick", "Sick leave", Decimal("0.5"), advance_only=False),
    LeaveType("unpaid", "Unpaid leave"),
]
ONSHORE_TYPES = [
    LeaveType("annual", "Annual leave"),
    LeaveType("personal", "Personal / carer's leave", advance_only=False),
    LeaveType("long_service", "Long service leave"),
    LeaveType("unpaid", "Unpaid leave"),
]
MAX_BACKDATE_DAYS = 60  # how far back personal/sick leave can be requested


def types_for(staff: Staff) -> list[LeaveType]:
    return OFFSHORE_TYPES if staff.location_type == "offshore" else ONSHORE_TYPES


def type_by_code(staff: Staff, code: str) -> LeaveType | None:
    return next((t for t in types_for(staff) if t.code == code), None)


# ---- dates ---------------------------------------------------------------------

def add_months(d: date, months: int) -> date:
    """Same day-of-month `months` later, clamped to the month's last day (31 Jan + 1 = 28/29 Feb)."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def accrual_dates(join: date, until: date) -> list[date]:
    out, k = [], PROBATION_MONTHS
    while (d := add_months(join, k)) <= until:
        out.append(d)
        k += 1
    return out


def count_days(start: date, end: date, start_half: bool = False, end_half: bool = False) -> Decimal:
    """Working days (Mon–Fri) in the range, with optional half first/last day."""
    if end < start:
        return Decimal(0)
    total = Decimal(0)
    d = start
    while d <= end:
        if d.weekday() < 5:
            total += 1
        d += timedelta(days=1)
    if start.weekday() < 5 and start_half:
        total -= Decimal("0.5")
    if end != start and end.weekday() < 5 and end_half:
        total -= Decimal("0.5")
    return max(total, Decimal(0))


# ---- balances --------------------------------------------------------------------

@dataclass
class Entry:
    date: date
    kind: str  # accrual | leave | set | add
    amount: Decimal  # change applied (accruals after the cap)
    balance: Decimal  # balance after this entry
    note: str = ""

    def to_dict(self) -> dict:
        return {"date": self.date.isoformat(), "kind": self.kind, "amount": float(self.amount),
                "balance": float(self.balance), "note": self.note}


@dataclass
class Balance:
    code: str
    label: str
    cap: Decimal | None
    balance: Decimal  # as of today (leave already started is deducted)
    booked: Decimal  # approved leave starting after today
    pending: Decimal  # waiting for approval
    next_date: date | None
    next_amount: Decimal | None
    history: list[Entry] = field(default_factory=list)

    @property
    def available(self) -> Decimal:
        """What's left to book: today's balance minus approved future leave."""
        return self.balance - self.booked

    def to_dict(self, history: int = 0) -> dict:
        return {
            "code": self.code, "label": self.label,
            "cap": float(self.cap) if self.cap is not None else None,
            "balance": float(self.balance), "booked": float(self.booked), "pending": float(self.pending),
            "available": float(self.available),
            "next_date": self.next_date.isoformat() if self.next_date else None,
            "next_amount": float(self.next_amount) if self.next_amount is not None else None,
            "history": [e.to_dict() for e in self.history[-history:]] if history else [],
        }


def compute_balance(
    lt: LeaveType,
    join: date,
    today: date,
    requests: list[LeaveRequest],
    adjustments: list[LeaveAdjustment],
) -> Balance:
    """Replay a balance's history up to today, plus what's booked and pending after today."""
    events: list[tuple[date, int, str, Decimal, str]] = []
    # Order within a day: that day's credit, then leave taken, then admin adjustments last,
    # so "set the balance to X on this date" is the final figure for that date.
    for d in accrual_dates(join, today):
        events.append((d, 0, "accrual", lt.accrual, ""))
    for r in requests:
        if r.leave_type == lt.code and r.status == "approved" and r.start_date <= today:
            events.append((r.start_date, 1, "leave", -Decimal(r.days), f"{r.start_date:%d %b}–{r.end_date:%d %b}"))
    for a in adjustments:
        if a.leave_type == lt.code and a.effective_date <= today:
            events.append((a.effective_date, 2, a.kind, Decimal(a.amount), a.reason))
    events.sort(key=lambda e: (e[0], e[1]))

    bal = Decimal(0)
    history: list[Entry] = []
    for d, _, kind, amount, note in events:
        if kind == "set":
            change = amount - bal
            bal = amount
        elif kind == "accrual" and lt.cap is not None:
            change = max(Decimal(0), min(amount, lt.cap - bal))
            bal += change
            if change < amount:
                note = f"at the {lt.cap:g}-day limit" if change == 0 else f"capped at {lt.cap:g}"
        else:
            change = amount
            bal += amount
        history.append(Entry(d, kind, change, bal, note))

    booked = sum((Decimal(r.days) for r in requests
                  if r.leave_type == lt.code and r.status == "approved" and r.start_date > today), Decimal(0))
    pending = sum((Decimal(r.days) for r in requests
                   if r.leave_type == lt.code and r.status == "pending"), Decimal(0))
    upcoming = accrual_dates(join, add_months(today, 1))
    nxt = next((d for d in upcoming if d > today), None)
    if nxt is None:  # still in probation: first credit is the 6-month anniversary
        nxt = add_months(join, PROBATION_MONTHS) if add_months(join, PROBATION_MONTHS) > today else None
    return Balance(lt.code, lt.label, lt.cap, bal, booked, pending, nxt, lt.accrual if nxt else None, history)


def balances_for(db: Session, staff: Staff, today: date) -> list[Balance] | None:
    """Offshore balances, or None if this app doesn't keep them (onshore / no join date)."""
    if staff.location_type != "offshore" or staff.join_date is None:
        return None
    requests = db.query(LeaveRequest).filter_by(staff_id=staff.staff_id).all()
    adjustments = db.query(LeaveAdjustment).filter_by(staff_id=staff.staff_id).all()
    return [compute_balance(t, staff.join_date, today, requests, adjustments)
            for t in OFFSHORE_TYPES if t.accrual is not None]
