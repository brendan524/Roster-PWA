"""Fortnightly pay report: what to enter into Xero for each person.

Pay follows the ROSTER; clock times check it.
- Fitness (casual / part-time): each shift pays from the later of rostered start
  and clock-in to the earlier of rostered end and clock-out (late = docked,
  extra time isn't paid unless approved), rounded to the nearest 15 minutes,
  minimum 1 hour. Overtime: over 10 hours in a day or 38 in a week (Mon–Sun).
  Public holiday hours go to their own item. First Aid Allowance = days worked.
  A day with more than one shift = 1 Broken Shift Allowance + 1 Excess Fares.
- First Aid (casual): Sessional Teaching = the roster's teaching hours (at least
  2 per course, at most 5 per day). Non-Teaching Duties = 0.5 hours per course
  plus travel time to client sites; Vehicle km for the same trips. First Aid and
  Laundry allowances = 1 per week (Mon–Sun) with at least one course worked.
  Courses outside 7am–7pm weekdays / 7am–12:30pm Saturdays, or on Sundays or
  public holidays, are flagged.
- Contractors: hours shown to check invoices; nothing to enter.
- Offshore (salaried): days worked, leave and problems only.
- Directors and auto-clocked staff are left out.

Quantities only: Xero holds the rates.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import holidays, travel
from app.config import get_settings
from app.leave import count_days, label_for
from app.models import LeaveRequest, PayReport, Punch, Staff, XeroEmployeeLink
from app.punches import analyse, effective_punches, local_day_bounds, pair_sessions, shift_bounds
from app.roster import RosterService, Shift

PERIOD_DAYS = 14
TEACH_MIN, TEACH_DAY_CAP, BUFFER = 2.0, 5.0, 0.5
FIT_MIN, FIT_DAY_OT, WEEK_OT = 1.0, 10.0, 38.0
TOLERANCE_MIN = 7  # clock differences under this aren't worth a note (rounding absorbs them)
MILESTONE = 800

# Xero pay item names
CASUAL_HOURS, ORDINARY_HOURS, PH_HOURS, OVERTIME = "Casual Ordinary Hours", "Ordinary Hours", "Public Holiday Hours", "Overtime"
TEACHING, NON_TEACHING, VEHICLE = "Sessional Teaching", "Non-Teaching Duties", "Vehicle Allowance (cents per KM)"
FIRST_AID_ALLOW, LAUNDRY, BROKEN, FARES = "First Aid Allowance", "Laundry Allowance", "Broken Shift Allowance", "Excess Fares Allowance"


# ---- periods -----------------------------------------------------------------------

def period_for(d: date) -> tuple[date, date]:
    """The fortnight (Mon–Sun) containing `d`."""
    anchor = date.fromisoformat(get_settings().pay_period_end)
    offset = (d - anchor).days
    end = anchor + timedelta(days=math.ceil(offset / PERIOD_DAYS) * PERIOD_DAYS)
    return end - timedelta(days=PERIOD_DAYS - 1), end


def pay_day(end: date) -> date:
    return end + timedelta(days=get_settings().pay_day_offset_days)


def round15(hours: float) -> float:
    return math.floor(hours * 4 + 0.5) / 4


def _h(td: timedelta) -> float:
    return td.total_seconds() / 3600


def _mins(td: timedelta) -> int:
    return round(td.total_seconds() / 60)


# ---- who's who -----------------------------------------------------------------------

def employment(staff: Staff, link: XeroEmployeeLink | None) -> str:
    """casual | part-time | full-time | contractor | salaried | director | unknown"""
    status = staff.employment_status.strip().lower()
    if staff.location_type == "offshore":
        return "salaried"
    if status.startswith("director") or staff.auto_clock:
        return "director"
    if not staff.has_leave:
        return "contractor"
    basis = link.employment_basis if link else ""
    if basis == "CASUAL" or status.startswith("casual"):
        return "casual"
    if basis == "PARTTIME" or status.startswith("part"):
        return "part-time"
    if basis == "FULLTIME" or status.startswith("full"):
        return "full-time"
    return "unknown"


def is_first_aid(shift: Shift) -> bool:
    return "first aid" in (shift.department or "").lower()


# ---- report ------------------------------------------------------------------------

@dataclass
class Person:
    staff_id: str
    name: str
    employment: str
    teams: set[str] = field(default_factory=set)
    items: dict[str, float] = field(default_factory=dict)
    shifts: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    leave: list[str] = field(default_factory=list)
    teaching_delivered: float = 0.0  # for the 800-hour tracker (uncapped, courses actually worked)

    def add(self, item: str, qty: float) -> None:
        if qty:
            self.items[item] = round(self.items.get(item, 0) + qty, 2)

    def out(self) -> dict:
        return {"staff_id": self.staff_id, "name": self.name, "employment": self.employment,
                "teams": sorted(self.teams), "items": [{"item": k, "qty": v} for k, v in self.items.items()],
                "shifts": self.shifts, "flags": self.flags, "leave": self.leave,
                "teaching_delivered": round(self.teaching_delivered, 2)}


def _fmt_t(dt: datetime | None, tz: ZoneInfo) -> str | None:
    return dt.astimezone(tz).strftime("%H:%M") if dt else None


def _day(d: date) -> str:
    return d.strftime("%a %-d %b")


def build(db: Session, service: RosterService, end: date, now: datetime | None = None, http=None) -> dict:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    now = now or datetime.now(UTC)
    start = end - timedelta(days=PERIOD_DAYS - 1)
    shifts = service.shifts_between(start - timedelta(days=1), end + timedelta(days=1))
    p_start, _ = local_day_bounds(start, tz)
    _, p_end = local_day_bounds(end, tz)
    punches = effective_punches(db, None, p_start - timedelta(hours=16), p_end + timedelta(hours=16))
    by_staff: dict[str, list[Punch]] = defaultdict(list)
    for p in punches:
        by_staff[p.staff_id].append(p)
    sessions = [x for ps in by_staff.values() for x in pair_sessions(ps)]
    analyse(sessions, shifts, tz, now)
    session_for = {id(x.shift): x for x in sessions if x.shift is not None}
    in_period = [sh for sh in shifts if start <= sh.date <= end]

    staff = {st.staff_id: st for st in db.query(Staff)}
    links = {l.staff_id: l for l in db.query(XeroEmployeeLink)}
    venues = travel.venues_for(db, {sh.location for sh in in_period if is_first_aid(sh) and sh.location})
    people: dict[str, Person] = {}

    def person(sid: str, fallback: str) -> Person:
        if sid not in people:
            st = staff.get(sid)
            people[sid] = Person(sid, st.name if st else fallback, employment(st, links.get(sid)) if st else "unknown")
        return people[sid]

    fa_days: dict[str, dict[date, list]] = defaultdict(lambda: defaultdict(list))
    fit_days: dict[str, dict[date, list]] = defaultdict(lambda: defaultdict(list))

    for sh in sorted(in_period, key=lambda x: (x.date, x.start)):
        pr = person(sh.staff_id, sh.staff_name)
        if pr.employment == "director":
            continue
        rs, re_ = shift_bounds(sh, tz)
        sess = session_for.get(id(sh))
        cin = sess.clock_in.occurred_at if sess and sess.clock_in else None
        cout = sess.clock_out.occurred_at if sess and sess.clock_out else None
        row = {"date": sh.date.isoformat(), "start": sh.start, "end": sh.end, "location": sh.location,
               "department": sh.department, "role": sh.role, "clock_in": _fmt_t(cin, tz), "clock_out": _fmt_t(cout, tz),
               "rostered_hours": round(_h(re_ - rs), 2), "notes": []}
        pr.shifts.append(row)
        ph = holidays.holiday(sh.date)
        if sess is None:
            row["paid_hours"] = 0
            if rs < now:
                row["notes"].append("Didn't clock in — not paid")
                pr.flags.append(f"{_day(sh.date)} {sh.start}–{sh.end}: no clock-in or clock-out (rostered, not paid)")
            continue
        if cin is None:
            row["notes"].append("No clock-in — rostered start used")
            pr.flags.append(f"{_day(sh.date)}: no clock-in — paid from the rostered start; check")
        if cout is None:
            row["notes"].append("No clock-out — rostered end used")
            pr.flags.append(f"{_day(sh.date)}: no clock-out — paid to the rostered end; check")
        a, b = max(rs, cin or rs), min(re_, cout or re_)
        late = _mins((cin or rs) - rs)
        early_leave = _mins(re_ - (cout or re_))
        extra = max(0, _mins(rs - (cin or rs))) + max(0, _mins((cout or re_) - re_))
        worked_inside = max(0.0, _h(b - a))
        row["clocked_hours"] = round15(_h((cout or re_) - (cin or rs))) if (cin or cout) else 0

        if pr.employment == "salaried":
            pr.teams.add("Offshore")
            row["paid_hours"] = None
            continue
        if is_first_aid(sh):
            pr.teams.add("First Aid")
            teaching = sh.teaching_hours
            if teaching is None:
                teaching = max(0.0, _h(re_ - rs) - BUFFER)
                pr.flags.append(f"{_day(sh.date)}: no Teaching Hours in the roster — assumed {teaching:g} h; check")
            row["teaching_rostered"] = teaching
            fa_days[sh.staff_id][sh.date].append((sh, teaching))
            pr.teaching_delivered += teaching
            if late > TOLERANCE_MIN:
                pr.flags.append(f"{_day(sh.date)}: clocked in {late} min after the rostered start")
            if early_leave > TOLERANCE_MIN:
                pr.flags.append(f"{_day(sh.date)}: clocked out {early_leave} min before the rostered end")
            if extra > 15:
                row["notes"].append(f"{extra} min clocked outside the roster (not paid unless approved)")
                pr.flags.append(f"{_day(sh.date)}: clocked {extra} min longer than the rostered shift")
            wd, st_t, en_t = sh.date.weekday(), time.fromisoformat(sh.start), time.fromisoformat(sh.end)
            outside = (wd == 6 or (wd < 5 and (st_t < time(7) or en_t > time(19) or sh.overnight))
                       or (wd == 5 and (st_t < time(7) or en_t > time(12, 30) or sh.overnight)))
            if outside:
                pr.flags.append(f"{_day(sh.date)} {sh.start}–{sh.end}: outside ordinary hours — check the rate")
            if ph:
                pr.flags.append(f"{_day(sh.date)}: {ph} (public holiday) — check the rate")
            continue

        # Fitness (and anything else hourly)
        pr.teams.add("Fitness")
        paid = round15(worked_inside)
        if 0 < paid < FIT_MIN:
            row["notes"].append("Topped up to the 1-hour minimum")
            paid = FIT_MIN
        row["paid_hours"] = paid
        if pr.employment != "contractor":
            docked = round15(_h(re_ - rs)) - paid
            if docked > 0 and (late > TOLERANCE_MIN or early_leave > TOLERANCE_MIN):
                why = [f"{late} min late" if late > TOLERANCE_MIN else "", f"left {early_leave} min early" if early_leave > TOLERANCE_MIN else ""]
                row["notes"].append(f"{' and '.join(w for w in why if w)} — {docked:g} h docked")
                pr.flags.append(f"{_day(sh.date)}: {' and '.join(w for w in why if w)} — {docked:g} h docked")
            if extra > TOLERANCE_MIN:
                row["notes"].append(f"{extra} min clocked outside the roster — not paid unless you approve")
                pr.flags.append(f"{_day(sh.date)}: {extra} min clocked outside the roster — not paid unless you approve")
        fit_days[sh.staff_id][sh.date].append((sh, paid, ph))

    # Clock-ins with no rostered shift
    for x in sessions:
        if x.shift is None and x.staff_id in staff:
            at = (x.clock_in or x.clock_out).occurred_at.astimezone(tz)
            if start <= at.date() <= end and employment(staff[x.staff_id], links.get(x.staff_id)) != "director":
                person(x.staff_id, x.staff_id).flags.append(
                    f"{_day(at.date())}: clocked {'in' if x.clock_in else 'out'} at {at:%H:%M} with no rostered shift — not paid")

    # Fitness totals
    for sid, days in fit_days.items():
        pr = people[sid]
        if pr.employment in ("contractor", "salaried"):
            continue
        ordinary = CASUAL_HOURS if pr.employment == "casual" else ORDINARY_HOURS
        if pr.employment == "unknown":
            pr.flags.insert(0, "Employment type unknown (casual / part-time?) — hours shown as Casual Ordinary Hours; check")
            ordinary = CASUAL_HOURS
        week_ord: dict[date, float] = defaultdict(float)
        for d in sorted(days):
            entries = days[d]
            day_ph = sum(p for _, p, ph in entries if ph)
            day_norm = sum(p for _, p, ph in entries if not ph)
            ot = max(0.0, day_norm - FIT_DAY_OT)
            day_norm -= ot
            wk = d - timedelta(days=d.weekday())
            room = max(0.0, WEEK_OT - week_ord[wk])
            if day_norm > room:
                ot += day_norm - room
                day_norm = room
            week_ord[wk] += day_norm
            pr.add(ordinary, day_norm)
            pr.add(PH_HOURS, day_ph)
            pr.add(OVERTIME, ot)
            if ot:
                pr.flags.append(f"{_day(d)}: {ot:g} h overtime (over 10 h in the day or 38 h in the week)")
            if day_ph:
                pr.flags.append(f"{_day(d)}: {holidays.holiday(d)} — {day_ph:g} h as Public Holiday Hours")
            worked = [e for e in entries if e[1] > 0]
            if worked:
                pr.add(FIRST_AID_ALLOW, 1)
            if len(worked) >= 2:
                pr.add(BROKEN, 1)
                pr.add(FARES, 1)

    # First Aid totals
    for sid, days in fa_days.items():
        pr = people[sid]
        if pr.employment in ("contractor", "salaried"):
            continue
        weeks = set()
        for d in sorted(days):
            entries = days[d]
            teach = sum(max(TEACH_MIN, t) for _, t in entries)
            if any(t < TEACH_MIN for _, t in entries):
                pr.flags.append(f"{_day(d)}: course under 2 teaching hours — paid the 2-hour minimum")
            if teach > TEACH_DAY_CAP:
                pr.flags.append(f"{_day(d)}: {teach:g} teaching hours — capped at the 5-hour daily rate")
                teach = TEACH_DAY_CAP
            elif teach == TEACH_DAY_CAP:
                pr.flags.append(f"{_day(d)}: 5 teaching hours — the full daily rate")
            pr.add(TEACHING, teach)
            pr.add(NON_TEACHING, BUFFER * len(entries))
            clients = []
            eligible = staff[sid].travel_eligible if sid in staff else False
            for sh, _t in entries:
                v = venues.get(travel.key(sh.location))
                row = next((r for r in pr.shifts if r["date"] == d.isoformat() and r["start"] == sh.start), None)
                if v is not None and v.kind == "campus":
                    if row is not None:
                        row["notes"].append("Campus — no travel")
                elif not eligible:
                    continue  # travel isn't paid for this trainer
                elif v is None or v.kind == "unknown":
                    pr.flags.append(f"{_day(d)}: venue “{sh.location or '(blank)'}” isn't set as a campus or client site — "
                                    "travel not included; set it under Clock → Pay report → First Aid venues")
                else:
                    clients.append(sh.location)
            if clients:
                # HQ → each client site in order → HQ, benchmarked from HQ (not where they actually drove from).
                trip = travel.day_travel(db, clients, venues, http)
                if trip.km is None:
                    pr.flags.append(f"{_day(d)}: travel to {', '.join(clients)} not included — {trip.problem}")
                else:
                    hours, km = round(trip.minutes / 60, 2), round(trip.km, 1)
                    pr.add(NON_TEACHING, hours)
                    pr.add(VEHICLE, km)
                    for row in pr.shifts:
                        if row["date"] == d.isoformat():
                            row["travel"] = {"km": km, "hours": hours}
                            break
            weeks.add(d - timedelta(days=d.weekday()))
        pr.add(FIRST_AID_ALLOW, len(weeks))
        pr.add(LAUNDRY, len(weeks))

    # Approved leave in the period
    for r in db.query(LeaveRequest).filter(LeaveRequest.status == "approved", LeaveRequest.start_date <= end,
                                           LeaveRequest.end_date >= start):
        st = staff.get(r.staff_id)
        if st is None:
            continue
        a, b = max(r.start_date, start), min(r.end_date, end)
        days_n = count_days(a, b, r.start_half and a == r.start_date, r.end_half and b == r.end_date)
        if days_n <= 0:
            continue
        pr = person(r.staff_id, st.name)
        if pr.employment == "director":
            continue
        span = _day(a) if a == b else f"{_day(a)} – {_day(b)}"
        pr.leave.append(f"{label_for(st, r.leave_type)}: {float(days_n):g} day{'s' if days_n != 1 else ''} ({span})")

    for pr in people.values():
        if pr.employment == "salaried":
            pr.items = {}
            worked = {row["date"] for row in pr.shifts if row.get("clock_in") or row.get("clock_out")}
            pr.items["Days worked"] = len(worked)

    order = {"casual": 0, "part-time": 0, "full-time": 0, "unknown": 0, "contractor": 1, "salaried": 2}
    out_people = sorted((p for p in people.values() if p.employment != "director"),
                        key=lambda p: (order.get(p.employment, 3), p.name.lower()))
    return {
        "period_start": start.isoformat(), "period_end": end.isoformat(), "pay_day": pay_day(end).isoformat(),
        "generated_at": now.isoformat(),
        "people": [p.out() for p in out_people],
        "tracker": teaching_tracker(db, staff, {p.staff_id: p.teaching_delivered for p in out_people}, end),
    }


def teaching_tracker(db: Session, staff: dict[str, Staff], this_period: dict[str, float], end: date) -> list[dict]:
    """First Aid trainers' total teaching hours: opening balance + saved reports + this report."""
    saved: dict[str, float] = defaultdict(float)
    for rep in db.query(PayReport).filter(PayReport.period_end < end):
        for p in json.loads(rep.data)["people"]:
            st = staff.get(p["staff_id"])
            if st and (st.teaching_hours_as_of is None or rep.period_start > st.teaching_hours_as_of):
                saved[p["staff_id"]] += p.get("teaching_delivered", 0)
    out = []
    ids = {sid for sid, h in this_period.items() if h} | {sid for sid, h in saved.items() if h} | \
          {st.staff_id for st in staff.values() if st.teaching_hours_opening}
    for sid in ids:
        st = staff.get(sid)
        if st is None or not st.active:
            continue
        before = (st.teaching_hours_opening or 0) + saved.get(sid, 0)
        total = before + this_period.get(sid, 0)
        crossed = int(total // MILESTONE) > int(before // MILESTONE)
        out.append({"staff_id": sid, "name": st.name, "total": round(total, 2), "level": int(total // MILESTONE) + 1,
                    "next_milestone": (int(total // MILESTONE) + 1) * MILESTONE,
                    "reached": int(total // MILESTONE) * MILESTONE if crossed else None,
                    "opening": st.teaching_hours_opening or 0,
                    "opening_as_of": st.teaching_hours_as_of.isoformat() if st.teaching_hours_as_of else None})
    return sorted(out, key=lambda x: x["name"].lower())


# ---- email ----------------------------------------------------------------------------

EMPLOYMENT_LABEL = {"casual": "Casual", "part-time": "Part-time", "full-time": "Full-time", "contractor": "Contractor",
                    "salaried": "Salaried (offshore)", "unknown": "Employment type unknown"}


def email_text(rep: dict) -> str:
    def d(x: str) -> str:
        return date.fromisoformat(x).strftime("%a %-d %b %Y")

    lines = [f"Pay report: {d(rep['period_start'])} to {d(rep['period_end'])}", f"Pay day: {d(rep['pay_day'])}", ""]
    checks = sum(len(p["flags"]) for p in rep["people"])
    lines.append(f"{checks} thing{'s' if checks != 1 else ''} to check (marked ⚠ below)." if checks else "Nothing flagged.")
    for group, title in ((("casual", "part-time", "full-time", "unknown"), "ENTER IN XERO"),
                         (("contractor",), "CONTRACTORS — check against their invoices"),
                         (("salaried",), "OFFSHORE — salaried")):
        ppl = [p for p in rep["people"] if p["employment"] in group]
        if not ppl:
            continue
        lines += ["", "=" * 44, title, "=" * 44]
        for p in ppl:
            lines += ["", f"{p['name']} ({p['staff_id']}) · {EMPLOYMENT_LABEL.get(p['employment'], p['employment'])}"
                      + (f" · {', '.join(p['teams'])}" if p["teams"] else "")]
            if p["employment"] == "contractor":
                worked = [s for s in p["shifts"] if s.get("clocked_hours")]
                hrs = sum(s["clocked_hours"] for s in worked)
                rostered = sum(s["rostered_hours"] for s in p["shifts"])
                lines.append(f"  Clocked {hrs:g} h over {len(worked)} shift(s) (rostered {rostered:g} h)")
            else:
                for it in p["items"]:
                    lines.append(f"  {it['item']:<34} {it['qty']:g}")
                if not p["items"]:
                    lines.append("  Nothing to pay this period")
            for lv in p["leave"]:
                lines.append(f"  Leave: {lv}")
            for f in p["flags"]:
                lines.append(f"  ⚠ {f}")
    if rep.get("tracker"):
        lines += ["", "=" * 44, "FIRST AID TEACHING HOURS (800-hour progression)", "=" * 44]
        for t in rep["tracker"]:
            extra = f"  ★ reached {t['reached']:,} hours this period" if t["reached"] else ""
            lines.append(f"  {t['name']:<26} {t['total']:,.1f} h · Level {t['level']} (next level at {t['next_milestone']:,} h){extra}")
    lines += ["", "Each shift's rostered and clocked times are in the app: Clock → Pay report."]
    return "\n".join(lines)
