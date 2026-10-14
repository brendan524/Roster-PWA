from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.roster import RosterService, RosterUnavailable
from tests.conftest import HEADER, FakeSource

TZ = ZoneInfo("Australia/Melbourne")


def make(src, now, ttl=120, min_refresh=0):
    svc = RosterService(src, "Australia/Melbourne", ttl, min_refresh)
    svc.now = lambda: now[0]
    return svc


def test_reads_current_and_next_month_and_flags_missing_current():
    src = FakeSource({"2026-11": [HEADER]})
    now = [datetime(2026, 10, 14, 9, tzinfo=TZ)]
    snap = make(src, now).get()
    assert [(m.name, m.exists) for m in snap.months] == [("2026-10", False), ("2026-11", True)]
    assert len(snap.issues) == 1 and "current month" in snap.issues[0].message


def test_missing_next_month_is_not_an_issue():
    src = FakeSource({"2026-10": [HEADER]})
    snap = make(src, [datetime(2026, 10, 14, tzinfo=TZ)]).get()
    assert snap.issues == []
    assert snap.months[1].exists is False


def test_caches_until_ttl_then_refetches_and_rolls_over_month():
    src = FakeSource({})
    now = [datetime(2026, 10, 31, 23, 58, tzinfo=TZ)]
    svc = make(src, now)
    svc.get()
    svc.get()
    assert src.calls == 1
    now[0] = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)  # month boundary: new pair of tabs
    snap = svc.get()
    assert src.calls == 2
    assert [m.name for m in snap.months] == ["2026-11", "2026-12"]


def test_force_refresh_is_throttled():
    src = FakeSource({})
    now = [datetime(2026, 10, 14, 9, tzinfo=TZ)]
    svc = make(src, now, min_refresh=20)
    svc.get()
    svc.get(force=True)
    assert src.calls == 1
    now[0] = now[0].replace(second=30)
    svc.get(force=True)
    assert src.calls == 2


def test_failure_serves_stale_copy_and_says_so():
    src = FakeSource({"2026-10": [HEADER]})
    now = [datetime(2026, 10, 14, 9, tzinfo=TZ)]
    svc = make(src, now)
    first = svc.get()
    src.fail = True
    snap = svc.get(force=True)
    assert snap.error and snap.fetched_at == first.fetched_at
    src.fail = False
    assert svc.get().error is None  # recovers on the next attempt


def test_failure_with_no_copy_raises():
    src = FakeSource()
    src.fail = True
    with pytest.raises(RosterUnavailable):
        make(src, [datetime(2026, 10, 14, tzinfo=TZ)]).get()


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("values:batchGet"):
            return FakeResp({"valueRanges": [{"range": "'2026-10'!A1:K3", "values": [["Date"], [46323]]}]})
        return FakeResp({"sheets": [{"properties": {"title": "2026-10 "}}, {"properties": {"title": "Template"}}]})


def test_google_source_lists_and_reads_tabs():
    from app.roster import GoogleSheetsSource

    src = GoogleSheetsSource("sheet123", "/nonexistent.json")
    http = FakeHttp()
    src._session = http
    assert src.tab_titles() == ["2026-10 ", "Template"]
    assert src.read_tabs(["2026-10 "]) == {"2026-10 ": [["Date"], [46323]]}
    _, params = http.calls[1]
    assert params["ranges"] == ["'2026-10 '"]  # exact title, quoted
    assert params["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert src.read_tabs([]) == {}


@pytest.mark.parametrize("title,month", [
    ("2026-10", "2026-10"), ("10/2026", "2026-10"), (" 1/2027 ", "2027-01"),
    ("13/2026", None), ("Copy of AB-Roster-Flat-Template", None), ("Oct 2026", None),
])
def test_month_of_title(title, month):
    from app.roster import month_of_title

    assert month_of_title(title) == month


def test_reads_mm_yyyy_tab_names_like_the_template():
    src = FakeSource({"10/2026": [HEADER, ["2026-10-28", "AB0010", "Sonya", "Fitness", "Cardio", "05:45", "06:45"]],
                      "11/2026": [HEADER], "Copy of AB-Roster-Flat-Template": [HEADER]})
    snap = make(src, [datetime(2026, 10, 15, tzinfo=TZ)]).get()
    assert [(m.name, m.exists) for m in snap.months] == [("2026-10", True), ("2026-11", True)]
    assert len(snap.shifts) == 1 and snap.issues == []


def test_two_tabs_for_the_same_month_is_an_error():
    src = FakeSource({"10/2026": [HEADER], "2026-10": [HEADER]})
    snap = make(src, [datetime(2026, 10, 15, tzinfo=TZ)]).get()
    assert snap.months[0].exists is False
    assert "More than one tab for 2026-10" in snap.issues[0].message


def test_wrong_month_rows_flagged_with_real_tab_title():
    src = FakeSource({"11/2026": [HEADER, ["2026-10-28", "AB0010", "Sonya", "Fitness", "Cardio", "05:45", "06:45"]]})
    snap = make(src, [datetime(2026, 10, 15, tzinfo=TZ)]).get()
    [w] = [i for i in snap.issues if i.level == "warning"]
    assert w.tab == "11/2026" and "isn't in this tab's month" in w.message


def test_same_shift_in_both_month_tabs_is_flagged():
    row = ["2026-10-28", "AB0010", "Sonya", "Fitness", "Cardio", "05:45", "06:45"]
    src = FakeSource({"10/2026": [HEADER, row], "11/2026": [HEADER, row]})
    snap = make(src, [datetime(2026, 10, 15, tzinfo=TZ)]).get()
    dup = [i for i in snap.issues if "staff will see it twice" in i.message]
    assert len(dup) == 1 and dup[0].tab == "11/2026" and "10/2026 row 2" in dup[0].message
