"""Read and validate the flat roster sheet.

The roster spreadsheet has one tab per month, named YYYY-MM (e.g. "2026-10").
We read the current month's tab and next month's tab. Each tab has a header row
and then one row per shift.

Shifts drive pay, so parsing is strict and "fails loud": any row we can't parse
becomes an *issue* that admins see, instead of a silently missing shift.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# ---- columns ---------------------------------------------------------------

# Canonical field -> accepted header spellings (after normalising: lower-case,
# punctuation collapsed to single spaces). Column order in the sheet doesn't matter.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "shift date"),
    "staff_id": ("staff id", "staffid", "staff no", "employee id"),
    "staff_name": ("staff name", "name", "staff"),
    "department": ("department", "dept"),
    "role": ("class role", "class", "role"),
    "start": ("shift start", "start", "start time"),
    "end": ("shift end", "end", "end time", "finish"),
    "location": ("location", "venue"),
    "scheduled_hours": ("scheduled hours", "hours"),
    "teaching_hours": ("teaching hours",),
    "notes": ("notes", "note", "comments"),
}
REQUIRED_COLUMNS = ("date", "staff_id", "start", "end")
COLUMN_LABELS = {
    "date": "Date",
    "staff_id": "Staff ID",
    "start": "Shift Start",
    "end": "Shift End",
}
HEADER_SEARCH_ROWS = 5  # header may sit below a title row or two


def _norm_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


_ALIAS_LOOKUP = {alias: key for key, aliases in COLUMN_ALIASES.items() for alias in aliases}


# ---- data ------------------------------------------------------------------

@dataclass(frozen=True)
class Shift:
    tab: str
    row: int  # 1-based row number in the sheet, so admins can find it
    date: date
    staff_id: str
    staff_name: str
    department: str
    role: str
    start: str  # "HH:MM", as written in the sheet (local time of the shift)
    end: str
    overnight: bool  # end is on the following day
    location: str
    scheduled_hours: float
    teaching_hours: float | None
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "staff_id": self.staff_id,
            "staff_name": self.staff_name,
            "department": self.department,
            "role": self.role,
            "start": self.start,
            "end": self.end,
            "overnight": self.overnight,
            "location": self.location,
            "scheduled_hours": self.scheduled_hours,
            "teaching_hours": self.teaching_hours,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Issue:
    level: str  # "error" = row NOT shown to anyone; "warning" = shown, but check it
    tab: str
    row: int | None
    message: str
    staff_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "tab": self.tab,
            "row": self.row,
            "message": self.message,
            "staff_id": self.staff_id,
        }


@dataclass
class MonthTab:
    name: str  # "2026-10"
    exists: bool
    shifts: list[Shift] = field(default_factory=list)


@dataclass
class RosterSnapshot:
    months: list[MonthTab]
    issues: list[Issue]
    fetched_at: datetime
    error: str | None = None  # set if the latest refresh failed and this is older data

    @property
    def shifts(self) -> list[Shift]:
        return [s for m in self.months for s in m.shifts]


# ---- cell parsing ----------------------------------------------------------

SHEETS_EPOCH = date(1899, 12, 30)  # Google Sheets / Excel serial day 0


class CellError(ValueError):
    pass


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def parse_date(value: Any) -> date:
    if isinstance(value, bool):
        raise CellError(f"'{value}' is not a date")
    if isinstance(value, (int, float)):
        if not 20000 < value < 80000:  # roughly 1954..2119; guards against stray numbers
            raise CellError(f"'{value}' is not a date")
        return SHEETS_EPOCH + timedelta(days=int(value))
    text = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        # Australian day-first format only (never month-first).
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text)
        if not m:
            raise CellError(f"'{text}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")
        d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        raise CellError(f"'{text}' is not a real date") from None


def parse_time(value: Any) -> time:
    if isinstance(value, bool):
        raise CellError(f"'{value}' is not a time")
    if isinstance(value, (int, float)):
        frac = float(value) % 1  # a datetime serial's fractional part is the time of day
        minutes = round(frac * 24 * 60)
        if minutes == 24 * 60:
            minutes = 0
        return time(minutes // 60, minutes % 60)
    text = str(value).strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm|a|p)?", text)
    if not m:
        m2 = re.fullmatch(r"(\d{1,2})\s*(am|pm|a|p)", text)
        if not m2:
            raise CellError(f"'{value}' is not a time (use 24-hour HH:MM, e.g. 05:45 or 17:30)")
        h, mi, ampm = int(m2.group(1)), 0, m2.group(2)
    else:
        h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(4)
    if ampm:
        if not 1 <= h <= 12:
            raise CellError(f"'{value}' is not a valid time")
        h = h % 12 + (12 if ampm.startswith("p") else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise CellError(f"'{value}' is not a valid time")
    return time(h, mi)


def parse_hours(value: Any) -> float:
    if isinstance(value, bool):
        raise CellError(f"'{value}' is not a number of hours")
    if isinstance(value, (int, float)):
        hours = float(value)
    else:
        text = str(value).strip()
        try:
            hours = float(text)
        except ValueError:
            raise CellError(f"'{text}' is not a number of hours (use decimals, e.g. 1.5)") from None
    if hours < 0 or hours > 24:
        raise CellError(f"'{value}' hours is out of range")
    return round(hours, 2)


def _text(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


# ---- tab parsing -----------------------------------------------------------

def _find_header(rows: list[list[Any]]) -> tuple[int, dict[str, int]] | None:
    """The header is the first row with a Date column and at least two other known columns."""
    for idx, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        mapping: dict[str, int] = {}
        for col, cell in enumerate(row):
            key = _ALIAS_LOOKUP.get(_norm_header(cell))
            if key and key not in mapping:
                mapping[key] = col
        if "date" in mapping and len(mapping) >= 3:
            return idx, mapping
    return None


def parse_tab(tab: str, rows: list[list[Any]]) -> tuple[list[Shift], list[Issue]]:
    """Parse one month tab into shifts plus issues. Never raises on bad data."""
    shifts: list[Shift] = []
    issues: list[Issue] = []

    if not any(any(not _is_blank(c) for c in r) for r in rows):
        return shifts, issues  # an empty tab is valid: no shifts yet

    found = _find_header(rows)
    if found is None:
        issues.append(Issue("error", tab, None,
                            "Couldn't find the header row (needs 'Date', 'Staff ID', 'Shift Start' and "
                            f"'Shift End' columns in the first {HEADER_SEARCH_ROWS} rows). No shifts read from this tab."))
        return shifts, issues
    header_idx, cols = found
    missing = [COLUMN_LABELS[c] for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        msg = f"Missing required column(s): {', '.join(missing)}. No shifts read from this tab."
        if "staff_id" not in cols and "staff_name" in cols:
            msg += " Shifts are matched by Staff ID (e.g. AB0010), not by name — add a Staff ID column."
        issues.append(Issue("error", tab, header_idx + 1, msg))
        return shifts, issues

    try:
        tab_year, tab_month = map(int, tab.split("-"))
    except ValueError:
        tab_year = tab_month = None

    seen: dict[tuple[str, date, str], int] = {}

    for idx in range(header_idx + 1, len(rows)):
        row = rows[idx]
        rownum = idx + 1
        if all(_is_blank(c) for c in row):
            continue

        def cell(key: str) -> Any:
            col = cols.get(key)
            return row[col] if col is not None and col < len(row) else None

        staff_id = _text(cell("staff_id")).upper() or None
        problems: list[str] = []
        for key in REQUIRED_COLUMNS:
            if _is_blank(cell(key)):
                problems.append(f"{COLUMN_LABELS[key]} is empty")
        if problems:
            issues.append(Issue("error", tab, rownum, "Shift not shown: " + "; ".join(problems) + ".", staff_id))
            continue

        try:
            shift_date = parse_date(cell("date"))
            start = parse_time(cell("start"))
            end = parse_time(cell("end"))
            scheduled = None if _is_blank(cell("scheduled_hours")) else parse_hours(cell("scheduled_hours"))
            teaching = None if _is_blank(cell("teaching_hours")) else parse_hours(cell("teaching_hours"))
        except CellError as exc:
            issues.append(Issue("error", tab, rownum, f"Shift not shown: {exc}.", staff_id))
            continue

        if start == end:
            issues.append(Issue("error", tab, rownum, "Shift not shown: Shift Start and Shift End are the same.", staff_id))
            continue

        warnings: list[str] = []
        overnight = end < start
        span = (datetime.combine(date.min, end) - datetime.combine(date.min, start)).seconds / 3600
        if overnight:
            warnings.append(f"Shift ends after midnight ({start:%H:%M}–{end:%H:%M}); check this is intended.")
        if scheduled is None:
            scheduled = round(span, 2)
        elif scheduled - span > 0.01:
            warnings.append(f"Scheduled Hours ({scheduled:g}) is more than the shift length ({span:.2f}h).")
        if teaching is not None:
            if teaching > scheduled + 0.001:
                warnings.append(f"Teaching Hours ({teaching:g}) is more than Scheduled Hours ({scheduled:g}).")
            if "first aid" not in _text(cell("department")).lower():
                warnings.append("Teaching Hours is set on a non-First Aid shift.")
        if tab_year and (shift_date.year, shift_date.month) != (tab_year, tab_month):
            warnings.append(f"Date {shift_date.isoformat()} isn't in this tab's month ({tab}).")

        key = (staff_id, shift_date, f"{start:%H:%M}")
        if key in seen:
            warnings.append(f"Looks like a duplicate of row {seen[key]} (same person, date and start time).")
        else:
            seen[key] = rownum

        for w in warnings:
            issues.append(Issue("warning", tab, rownum, w, staff_id))

        shifts.append(Shift(
            tab=tab,
            row=rownum,
            date=shift_date,
            staff_id=staff_id,
            staff_name=_text(cell("staff_name")),
            department=_text(cell("department")),
            role=_text(cell("role")),
            start=f"{start:%H:%M}",
            end=f"{end:%H:%M}",
            overnight=overnight,
            location=_text(cell("location")),
            scheduled_hours=scheduled,
            teaching_hours=teaching,
            notes=_text(cell("notes")),
        ))

    shifts.sort(key=lambda s: (s.date, s.start, s.staff_id))
    return shifts, issues


def staff_issues(shifts: list[Shift], known: dict[str, bool]) -> list[Issue]:
    """Issues that depend on the staff table: unknown or inactive Staff IDs.

    `known` maps staff_id -> active. Checked at request time because staff change
    independently of the sheet.
    """
    out = []
    for s in shifts:
        if s.staff_id not in known:
            out.append(Issue("error", s.tab, s.row,
                             f"Unknown Staff ID '{s.staff_id}' — nobody can see this shift.", s.staff_id))
        elif not known[s.staff_id]:
            out.append(Issue("warning", s.tab, s.row,
                             f"Staff ID '{s.staff_id}' is marked inactive but has a shift.", s.staff_id))
    return out


# ---- month tabs ------------------------------------------------------------

def month_tab(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def roster_months(today: date) -> list[str]:
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return [month_tab(today), month_tab(nxt)]


# ---- sources ---------------------------------------------------------------

class RosterSource(Protocol):
    def read_tabs(self, names: list[str]) -> dict[str, list[list[Any]] | None]:
        """Return rows for each requested tab, or None for a tab that doesn't exist."""


class GoogleSheetsSource:
    """Reads tabs via the Google Sheets API using a read-only service account."""

    API = "https://sheets.googleapis.com/v4/spreadsheets"
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

    def __init__(self, sheet_id: str, key_file: str):
        if not sheet_id:
            raise RuntimeError("ROSTER_SHEET_ID is not set")
        self.sheet_id = sheet_id
        self.key_file = key_file
        self._session = None

    def _http(self):
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(self.key_file, scopes=self.SCOPES)
            self._session = AuthorizedSession(creds)
        return self._session

    def read_tabs(self, names: list[str]) -> dict[str, list[list[Any]] | None]:
        http = self._http()
        meta = http.get(f"{self.API}/{self.sheet_id}", params={"fields": "sheets.properties.title"}, timeout=15)
        meta.raise_for_status()
        titles = {s["properties"]["title"].strip(): s["properties"]["title"] for s in meta.json().get("sheets", [])}

        result: dict[str, list[list[Any]] | None] = {n: None for n in names}
        present = [n for n in names if n in titles]
        if not present:
            return result
        ranges = ["'" + titles[n].replace("'", "''") + "'" for n in present]
        resp = http.get(
            f"{self.API}/{self.sheet_id}/values:batchGet",
            params={
                "ranges": ranges,
                "valueRenderOption": "UNFORMATTED_VALUE",
                "dateTimeRenderOption": "SERIAL_NUMBER",
                "majorDimension": "ROWS",
            },
            timeout=20,
        )
        resp.raise_for_status()
        for name, vr in zip(present, resp.json().get("valueRanges", []), strict=True):
            result[name] = vr.get("values", [])
        return result


class CsvDirectorySource:
    """Local stand-in for the sheet: a folder of YYYY-MM.csv files. For dev and demos."""

    def __init__(self, directory: str):
        self.directory = Path(directory)

    def read_tabs(self, names: list[str]) -> dict[str, list[list[Any]] | None]:
        out: dict[str, list[list[Any]] | None] = {}
        for name in names:
            path = self.directory / f"{name}.csv"
            if path.exists():
                with path.open(newline="", encoding="utf-8-sig") as fh:
                    out[name] = list(csv.reader(fh))
            else:
                out[name] = None
        return out


# ---- cached service --------------------------------------------------------

class RosterUnavailable(RuntimeError):
    pass


class RosterService:
    """Caches the parsed roster for a short time so phones don't hammer the Sheets API."""

    def __init__(self, source: RosterSource, tz: str, ttl_seconds: int, min_refresh_seconds: int):
        self.source = source
        self.tz = ZoneInfo(tz)
        self.ttl = timedelta(seconds=ttl_seconds)
        self.min_refresh = timedelta(seconds=min_refresh_seconds)
        self._lock = threading.Lock()
        self._snapshot: RosterSnapshot | None = None
        self._last_attempt: datetime | None = None

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def get(self, force: bool = False) -> RosterSnapshot:
        with self._lock:
            now = self.now()
            months = roster_months(now.date())
            snap = self._snapshot
            fresh = (
                snap is not None
                and snap.error is None
                and [m.name for m in snap.months] == months
                and now - snap.fetched_at < self.ttl
            )
            throttled = self._last_attempt is not None and now - self._last_attempt < self.min_refresh
            if snap is not None and (fresh and not force or throttled):
                return snap

            self._last_attempt = now
            try:
                tabs = self.source.read_tabs(months)
            except Exception as exc:  # network, auth, quota...
                log.exception("Roster refresh failed")
                if snap is None:
                    raise RosterUnavailable("The roster couldn't be loaded right now.") from exc
                self._snapshot = RosterSnapshot(snap.months, snap.issues, snap.fetched_at,
                                                error=f"Latest refresh failed ({type(exc).__name__}); showing earlier copy.")
                return self._snapshot

            month_tabs: list[MonthTab] = []
            issues: list[Issue] = []
            for name in months:
                rows = tabs.get(name)
                if rows is None:
                    month_tabs.append(MonthTab(name, exists=False))
                    if name == months[0]:
                        issues.append(Issue("error", name, None,
                                            f"There's no '{name}' tab in the roster sheet for the current month."))
                    continue
                shifts, tab_issues = parse_tab(name, rows)
                month_tabs.append(MonthTab(name, exists=True, shifts=shifts))
                issues.extend(tab_issues)

            self._snapshot = RosterSnapshot(month_tabs, issues, now)
            return self._snapshot
