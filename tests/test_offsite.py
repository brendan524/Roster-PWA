from datetime import UTC, datetime, timedelta

import pytest

from app.offsite import OffsiteError, list_backups, prune, upload


class Resp:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._data


class FakeDrive:
    """Just enough of the Drive v3 API: list (paged), resumable upload, delete."""

    def __init__(self, files=None, page_size=3, size_override=None):
        self.files = list(files or [])
        self.page_size = page_size
        self.size_override = size_override
        self.deleted = []
        self.uploaded = None

    def get(self, url, params=None, timeout=None):
        assert params["supportsAllDrives"] == "true" and "'F1' in parents" in params["q"]
        start = int(params.get("pageToken", 0))
        page = sorted(self.files, key=lambda f: f["createdTime"], reverse=True)[start:start + self.page_size]
        nxt = start + self.page_size
        return Resp(data={"files": page, **({"nextPageToken": str(nxt)} if nxt < len(self.files) else {})})

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        assert params["uploadType"] == "resumable" and json["parents"] == ["F1"]
        self.meta = json
        return Resp(headers={"Location": "https://upload/session/1"})

    def put(self, url, data=None, headers=None, timeout=None):
        self.uploaded = data
        size = self.size_override if self.size_override is not None else len(data)
        return Resp(data={"id": "new", "name": self.meta["name"], "size": str(size)})

    def delete(self, url, params=None, timeout=None):
        self.deleted.append(url.rsplit("/", 1)[1])
        return Resp(204)


def files(n, now, start_days_ago=0):
    return [{"id": f"id{i}", "name": f"roster-{i}.sql.gz.gpg", "size": "100",
             "createdTime": (now - timedelta(days=start_days_ago + i)).isoformat().replace("+00:00", "Z")}
            for i in range(n)]


def test_upload_sends_bytes_and_checks_size():
    drive = FakeDrive()
    info = upload(drive, "F1", "roster-x.sql.gz.gpg", b"secret-bytes")
    assert drive.uploaded == b"secret-bytes" and info["name"] == "roster-x.sql.gz.gpg"


def test_upload_size_mismatch_is_a_failure():
    with pytest.raises(OffsiteError, match="size mismatch"):
        upload(FakeDrive(size_override=3), "F1", "roster-x", b"secret-bytes")


def test_empty_upload_refused():
    with pytest.raises(OffsiteError, match="empty"):
        upload(FakeDrive(), "F1", "roster-x", b"")


def test_listing_follows_pages():
    now = datetime(2026, 10, 15, tzinfo=UTC)
    assert len(list_backups(FakeDrive(files(8, now)), "F1")) == 8


def test_prune_removes_only_old_copies_and_always_keeps_seven():
    now = datetime(2026, 10, 15, tzinfo=UTC)
    drive = FakeDrive(files(100, now))  # one per day, 0..99 days old
    removed = prune(drive, "F1", keep_days=90, now=now)
    assert sorted(drive.deleted) == sorted(f"id{i}" for i in range(91, 100))
    assert len(removed) == 9


def test_prune_keeps_newest_seven_even_if_uploads_stopped_long_ago():
    now = datetime(2026, 10, 15, tzinfo=UTC)
    drive = FakeDrive(files(10, now, start_days_ago=200))  # all ancient
    prune(drive, "F1", keep_days=90, now=now)
    assert sorted(drive.deleted) == ["id7", "id8", "id9"]


def test_api_errors_are_reported_clearly():
    class Denied(FakeDrive):
        def get(self, url, params=None, timeout=None):
            return Resp(403, text='{"error": "The caller does not have permission"}')

    with pytest.raises(OffsiteError, match="403"):
        list_backups(Denied(), "F1")
