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


def test_google_source_reads_existing_tabs_and_reports_missing():
    from app.roster import GoogleSheetsSource

    src = GoogleSheetsSource("sheet123", "/nonexistent.json")
    http = FakeHttp()
    src._session = http
    out = src.read_tabs(["2026-10", "2026-11"])
    assert out == {"2026-10": [["Date"], [46323]], "2026-11": None}
    _, params = http.calls[1]
    assert params["ranges"] == ["'2026-10 '"]  # exact title (stray space tolerated), quoted
    assert params["valueRenderOption"] == "UNFORMATTED_VALUE"
