"""Availability and roster clashes.

Casual and contract staff (onshore) record specific dates/times they CAN'T work.
Everyone else (part-time, full-time, offshore) applies for leave instead.
Nobody approves availability; admins see it, and any rostered shift that falls
in an unavailable time, or on a day of approved/requested leave, is shown as a
clash (in the app and in the daily admin email).

Times are in the person's own local time; roster times (Melbourne) are
converted before comparing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AvailabilityDate, AvailabilityMonth, LeaveRequest, Staff, XeroEmployeeLink
from app.roster import Shift

MAX_RANGE_DAYS = 92


def fmt_t(t: time) -> str:
    return t.strftime("%-I:%M%p").lower().replace(":00", "")


def window_text(start: time | None, end: time | None) -> str:
    return "all day" if start is None else f"{fmt_t(start)}–{fmt_t(end)}"


def staff_tz(staff: Staff | None) -> ZoneInfo:
    """Availability is entered (and stored) in the person's own local time."""
    st = get_settings()
    return ZoneInfo(st.offshore_timezone if staff is not None and staff.location_type == "offshore" else st.timezone)


def _shift_span(shift: Shift, sheet_tz: ZoneInfo, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(shift.date, time.fromisoformat(shift.start), sheet_tz)
    end = datetime.combine(shift.date + timedelta(days=1 if shift.overnight else 0), time.fromisoformat(shift.end), sheet_tz)
    return start.astimezone(tz), end.astimezone(tz)


def _local_days(start: datetime, end: datetime) -> list[date]:
    days, d = [], start.date()
    while d <= end.date():
        days.append(d)
        d += timedelta(days=1)
    return days


def _hits(span: tuple[datetime, datetime], day: date, start: time | None, end: time | None, tz: ZoneInfo) -> bool:
    """Does the shift overlap this unavailable window on this local day?"""
    w_start = datetime.combine(day, start or time(0), tz)
    w_end = datetime.combine(day, end, tz) if end else datetime.combine(day + timedelta(days=1), time(0), tz)
    return span[0] < w_end and w_start < span[1]


def _covers(span: tuple[datetime, datetime], day: date, start: time | None, end: time | None, tz: ZoneInfo) -> bool:
    """Is the whole shift inside this available window on this local day?"""
    w_start = datetime.combine(day, start or time(0), tz)
    w_end = datetime.combine(day, end, tz) if end else datetime.combine(day + timedelta(days=1), time(0), tz)
    return w_start <= span[0] and span[1] <= w_end


def month_of(d: date) -> str:
    return f"{d:%Y-%m}"


def next_month(d: date) -> str:
    return month_of(date(d.year + (d.month == 12), d.month % 12 + 1, 1))


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def month_label(month: str) -> str:
    return month_bounds(month)[0].strftime("%B %Y")


def entries_for_month(db: Session, staff_id: str, month: str, kind: str | None = None) -> list[AvailabilityDate]:
    first, last = month_bounds(month)
    q = db.query(AvailabilityDate).filter(AvailabilityDate.staff_id == staff_id, AvailabilityDate.end_date >= first,
                                          AvailabilityDate.start_date <= last)
    if kind:
        q = q.filter(AvailabilityDate.kind == kind)
    return q.all()


def entry_text(a: AvailabilityDate) -> str:
    days = a.start_date.strftime("%a %-d %b") if a.start_date == a.end_date else \
        f"{a.start_date:%a %-d %b} – {a.end_date:%a %-d %b}"
    return f"{days}, {window_text(a.start_time, a.end_time)}" + (f" — {a.note}" if a.note else "")


def entries_text(rows: list[AvailabilityDate]) -> str:
    rows = sorted(rows, key=lambda a: (a.start_date, a.start_time or time(0)))
    return "\n".join(f"  • {entry_text(a)}" for a in rows) if rows else "  (nothing entered)"


@dataclass
class Clash:
    shift: Shift
    reason: str
    kind: str  # "date" | "not_available" | "leave" | "leave_pending"

    def to_dict(self, name: str) -> dict:
        return self.shift.to_dict() | {"name": name, "reason": self.reason, "kind": self.kind}


def load(db: Session, staff_ids: list[str] | None, start: date, end: date):
    dq = db.query(AvailabilityDate).filter(AvailabilityDate.end_date >= start, AvailabilityDate.start_date <= end)
    lq = db.query(LeaveRequest).filter(LeaveRequest.status.in_(("approved", "pending")),
                                       LeaveRequest.end_date >= start, LeaveRequest.start_date <= end)
    if staff_ids is not None:
        dq = dq.filter(AvailabilityDate.staff_id.in_(staff_ids))
        lq = lq.filter(LeaveRequest.staff_id.in_(staff_ids))
    dates: dict[str, list[AvailabilityDate]] = {}
    for d in dq:
        dates.setdefault(d.staff_id, []).append(d)
    leave: dict[str, list[LeaveRequest]] = {}
    for r in lq:
        leave.setdefault(r.staff_id, []).append(r)
    return dates, leave


def applies_to(db: Session, staff: Staff) -> bool:
    """Only onshore casuals and contractors set availability; everyone else applies for leave."""
    if staff.location_type != "onshore":
        return False
    if not staff.has_leave or staff.employment_status.strip().lower().startswith(("casual", "contract")):
        return True
    link = db.get(XeroEmployeeLink, staff.staff_id)
    return bool(link and link.employment_basis == "CASUAL")


def clashes(db: Session, shifts: list[Shift], start: date, end: date, staff_ids: list[str] | None = None,
            leave_labels: dict[str, str] | None = None) -> list[Clash]:
    """Rostered shifts between start and end (inclusive) that clash with availability or leave."""
    shifts = [s for s in shifts if start <= s.date <= end and (staff_ids is None or s.staff_id in staff_ids)]
    if not shifts:
        return []
    sheet_tz = ZoneInfo(get_settings().timezone)
    people = {s.staff_id: s for s in db.query(Staff).filter(Staff.staff_id.in_({sh.staff_id for sh in shifts}))}
    dates, leave = load(db, staff_ids, start - timedelta(days=1), end + timedelta(days=1))
    submitted: dict[str, set[str]] = {}
    for m in db.query(AvailabilityMonth).filter(AvailabilityMonth.staff_id.in_(list(people))):
        submitted.setdefault(m.staff_id, set()).add(m.month)
    out: list[Clash] = []
    for sh in sorted(shifts, key=lambda s: (s.date, s.start, s.staff_name)):
        tz = staff_tz(people.get(sh.staff_id))
        span = _shift_span(sh, sheet_tz, tz)
        days = _local_days(*span)
        found = None
        for r in leave.get(sh.staff_id, []):
            if any(r.start_date <= d <= r.end_date for d in days):
                label = (leave_labels or {}).get(r.leave_type, r.leave_type.replace("_", " "))
                half = (r.start_half and r.start_date in days) or (r.end_half and r.end_date in days)
                found = (f"{'Approved' if r.status == 'approved' else 'Requested'} {label.lower()}"
                         + (" (half day)" if half else ""), "leave" if r.status == "approved" else "leave_pending")
                break
        person = people.get(sh.staff_id)
        if found is None and person is not None and person.availability_mode == "available" \
                and month_of(days[0]) in submitted.get(sh.staff_id, set()):
            avail = [a for a in dates.get(sh.staff_id, []) if a.kind == "available" and a.start_date <= days[0] <= a.end_date]
            if not any(_covers(span, days[0], a.start_time, a.end_time, tz) for a in avail):
                found = ((f"Only available {', '.join(window_text(a.start_time, a.end_time) for a in avail)} that day"
                          if avail else "Not in their submitted availability"), "not_available")
        if found is None:
            for a in dates.get(sh.staff_id, []):
                if a.kind != "unavailable":
                    continue
                if any(a.start_date <= d <= a.end_date and _hits(span, d, a.start_time, a.end_time, tz) for d in days):
                    found = (f"Unavailable {window_text(a.start_time, a.end_time)}"
                             + (f" — “{a.note}”" if a.note else ""), "date")
                    break
        if found:
            out.append(Clash(sh, *found))
    return out


def dates_out(rows: list[AvailabilityDate]) -> list[dict]:
    return [{"id": d.id, "start_date": d.start_date.isoformat(), "end_date": d.end_date.isoformat(),
             "start": d.start_time.strftime("%H:%M") if d.start_time else None,
             "end": d.end_time.strftime("%H:%M") if d.end_time else None,
             "note": d.note, "kind": d.kind, "created_at": d.created_at.isoformat() if d.created_at else None}
            for d in sorted(rows, key=lambda d: (d.start_date, d.start_time or time(0)))]


def upcoming_dates(db: Session, staff_id: str, today: date) -> list[AvailabilityDate]:
    return db.query(AvailabilityDate).filter(AvailabilityDate.staff_id == staff_id,
                                             AvailabilityDate.end_date >= today).all()


def horizon(today: date, days: int = 14) -> tuple[date, date]:
    return today, today + timedelta(days=days - 1)
