from datetime import date, time

import pytest

from app.roster import CellError, parse_date, parse_hours, parse_tab, parse_time, roster_months, staff_issues
from tests.conftest import HEADER


def row(d="2026-10-28", sid="AB0003", start="05:45", end="06:45", dept="Gym", sched="1.00", teach="", notes=""):
    return [d, sid, "Sonya", dept, "Strength & Conditioning", start, end, "Tullamarine Gym", sched, teach, notes]


@pytest.mark.parametrize("value,expected", [
    ("2026-10-28", date(2026, 10, 28)),
    ("28/10/2026", date(2026, 10, 28)),
    ("3/11/2026", date(2026, 11, 3)),  # day-first, never US month-first
    (46323, date(2026, 10, 28)),  # Google Sheets serial number
    (46323.0, date(2026, 10, 28)),
])
def test_parse_date(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("value", ["31/02/2026", "10/28/2026", "next tues", "", 5, True])
def test_parse_date_rejects(value):
    with pytest.raises(CellError):
        parse_date(value)


@pytest.mark.parametrize("value,expected", [
    ("05:45", time(5, 45)),
    ("5:45", time(5, 45)),
    ("17:30:00", time(17, 30)),
    ("5:45 pm", time(17, 45)),
    ("12:00am", time(0, 0)),
    ("12:15 PM", time(12, 15)),
    ("6pm", time(18, 0)),
    ("5.45", time(5, 45)),
    (0.239583333, time(5, 45)),  # serial fraction of a day
    (46323.75, time(18, 0)),  # full datetime serial
])
def test_parse_time(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["25:00", "13:00 pm", "5:7", "morning", "0545"])
def test_parse_time_rejects(value):
    with pytest.raises(CellError):
        parse_time(value)


def test_parse_hours():
    assert parse_hours("1.5") == 1.5
    assert parse_hours(2) == 2.0
    with pytest.raises(CellError):
        parse_hours("1:30")
    with pytest.raises(CellError):
        parse_hours("-1")


def test_good_row():
    shifts, issues = parse_tab("2026-10", [HEADER, row()])
    assert issues == []
    [s] = shifts
    assert (s.staff_id, s.date, s.start, s.end, s.scheduled_hours, s.row) == (
        "AB0003", date(2026, 10, 28), "05:45", "06:45", 1.0, 2)


def test_column_order_and_title_rows_dont_matter():
    header = ["Notes", "Shift End", "Shift Start", "Staff ID", "Date"]
    rows = [["AB Roster — October"], [], header, ["", "6:45", "5:45", "ab0003", "28/10/2026"]]
    shifts, issues = parse_tab("2026-10", rows)
    assert issues == []
    assert shifts[0].staff_id == "AB0003"  # normalised to upper case
    assert shifts[0].scheduled_hours == 1.0  # computed from times when blank
    assert shifts[0].row == 4


def test_empty_tab_is_fine():
    assert parse_tab("2026-11", []) == ([], [])
    assert parse_tab("2026-11", [HEADER]) == ([], [])
    assert parse_tab("2026-11", [HEADER, ["", ""], []]) == ([], [])


def test_missing_required_column_flags_whole_tab():
    header = [h for h in HEADER if h != "Shift End"]
    shifts, issues = parse_tab("2026-10", [header, row()[:6]])
    assert shifts == []
    assert issues[0].level == "error" and "Shift End" in issues[0].message


def test_no_header_flags_tab():
    shifts, issues = parse_tab("2026-10", [["junk"], ["more junk"]])
    assert shifts == [] and issues[0].level == "error"


def test_bad_rows_are_flagged_not_dropped_silently():
    rows = [HEADER,
            row(start=""),                 # missing start
            row(d="31/02/2026"),           # bad date
            row(end="7.75pm"),             # bad time
            row(start="9:00", end="9:00"),  # zero length
            row(sid=""),                   # missing staff id
            row()]                         # fine
    shifts, issues = parse_tab("2026-10", rows)
    assert len(shifts) == 1
    errors = [i for i in issues if i.level == "error"]
    assert [i.row for i in errors] == [2, 3, 4, 5, 6]
    assert all(i.message.startswith("Shift not shown") for i in errors)


def test_warnings_keep_the_shift():
    rows = [HEADER,
            row(start="22:00", end="02:00", sched="4"),             # overnight
            row(sched="3"),                                        # more than shift length
            row(d="2026-11-02"),                                   # wrong month tab
            row(dept="First Aid", sched="1", teach="1.5", start="09:00", end="10:00"),  # teaching > scheduled
            row(dept="Gym", teach="0.5", start="11:00", end="12:00"),                  # teaching on non-FA
            row(start="13:00", end="14:00"), row(start="13:00", end="14:00")]          # duplicate
    shifts, issues = parse_tab("2026-10", rows)
    assert len(shifts) == 7
    assert all(i.level == "warning" for i in issues)
    msgs = " | ".join(i.message for i in issues)
    for needle in ("after midnight", "more than the shift length", "isn't in this tab's month",
                   "more than Scheduled Hours", "non-First Aid", "duplicate of row 7"):
        assert needle in msgs
    assert shifts[0].overnight is True or any(s.overnight for s in shifts)


def test_scheduled_less_than_span_is_ok_for_breaks():
    _, issues = parse_tab("2026-10", [HEADER, row(start="09:00", end="17:00", sched="7.5")])
    assert issues == []


def test_staff_issues():
    shifts, _ = parse_tab("2026-10", [HEADER, row(sid="AB0003"), row(sid="AB9999"), row(sid="AB0004")])
    issues = staff_issues(shifts, {"AB0003": True, "AB0004": False})
    assert sorted((i.level, i.staff_id) for i in issues) == [("error", "AB9999"), ("warning", "AB0004")]


def test_roster_months():
    assert roster_months(date(2026, 10, 14)) == ["2026-10", "2026-11"]
    assert roster_months(date(2026, 12, 31)) == ["2026-12", "2027-01"]


def test_name_only_sheet_explains_staff_id_is_needed():
    header = [h for h in HEADER if h != "Staff ID"]
    shifts, issues = parse_tab("2026-10", [header, ["2026-10-19", "Sonya", "Gym", "Cardio", "05:45", "06:45"]])
    assert shifts == []
    [issue] = issues
    assert "Missing required column(s): Staff ID" in issue.message and "not by name" in issue.message
