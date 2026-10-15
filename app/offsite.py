"""Copy encrypted backups to a Google Shared Drive (off the VPS).

Runs inside the app container, which already has the Google service account key.
The backup file arrives on stdin, already encrypted by deploy/backup.sh:

    gpg ... | docker compose exec -T roster-app python -m app.offsite upload roster-2026-10-15.sql.gz.gpg
    docker compose exec roster-app python -m app.offsite check    # test access / list recent copies

The service account must be a "Content manager" member of the Shared drive.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta

from app.config import get_settings

API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = ["https://www.googleapis.com/auth/drive"]
PREFIX = "roster-"


class OffsiteError(RuntimeError):
    pass


def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        get_settings().google_service_account_file, scopes=SCOPES)
    return AuthorizedSession(creds)


def _folder() -> str:
    folder = get_settings().backup_drive_folder_id
    if not folder:
        raise OffsiteError("BACKUP_DRIVE_FOLDER_ID is not set")
    return folder


def _check(resp, what: str):
    if resp.status_code >= 400:
        detail = resp.text[:300].replace("\n", " ")
        raise OffsiteError(f"{what} failed ({resp.status_code}): {detail}")
    return resp


def list_backups(http, folder: str) -> list[dict]:
    files, token = [], None
    while True:
        params = {
            "q": f"'{folder}' in parents and trashed = false and name contains '{PREFIX}'",
            "fields": "nextPageToken, files(id, name, size, createdTime)",
            "orderBy": "createdTime desc",
            "pageSize": 200,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
        }
        if token:
            params["pageToken"] = token
        data = _check(http.get(API, params=params, timeout=30), "Listing backups").json()
        files += data.get("files", [])
        token = data.get("nextPageToken")
        if not token:
            return files


def upload(http, folder: str, name: str, data: bytes) -> dict:
    if not data:
        raise OffsiteError("Nothing to upload (empty input)")
    start = _check(http.post(
        UPLOAD,
        params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id, name, size"},
        json={"name": name, "parents": [folder], "mimeType": "application/octet-stream"},
        headers={"X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(len(data))},
        timeout=30,
    ), "Starting upload")
    session_url = start.headers.get("Location")
    if not session_url:
        raise OffsiteError("Upload session URL missing")
    done = _check(http.put(session_url, data=data, headers={"Content-Type": "application/octet-stream"},
                           timeout=300), "Uploading")
    info = done.json()
    if int(info.get("size", -1)) != len(data):
        raise OffsiteError(f"Uploaded size mismatch: sent {len(data)} bytes, Drive has {info.get('size')}")
    return info


def prune(http, folder: str, keep_days: int, now: datetime | None = None) -> list[str]:
    """Delete copies older than keep_days, but never the newest few (in case uploads stopped)."""
    now = now or datetime.now(UTC)
    files = list_backups(http, folder)  # newest first
    cutoff = now - timedelta(days=keep_days)
    removed = []
    for f in files[7:]:  # always keep the 7 most recent, whatever their age
        created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
        if created < cutoff:
            _check(http.delete(f"{API}/{f['id']}", params={"supportsAllDrives": "true"}, timeout=30),
                   f"Deleting {f['name']}")
            removed.append(f["name"])
    return removed


def _record_success(name: str) -> None:
    """Note the time, so the app can email an alert if backups stop arriving (e.g. the nightly job stopped)."""
    try:
        from app import state
        from app.db import SessionLocal

        with SessionLocal() as db:
            state.put(db, "last_offsite_backup", datetime.now(UTC).isoformat())
            db.commit()
    except Exception as e:  # noqa: BLE001 — the upload itself worked
        print(f"note: couldn't record the backup time ({type(e).__name__})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.offsite")
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="upload stdin as NAME, then remove old copies")
    up.add_argument("name")
    sub.add_parser("check", help="test access and list the most recent copies")
    a = p.parse_args(argv)

    try:
        http = _session()
        folder = _folder()
        if a.cmd == "check":
            files = list_backups(http, folder)
            print(f"OK: can reach the backup folder. {len(files)} backup(s) there.")
            for f in files[:5]:
                print(f"  {f['createdTime'][:16].replace('T', ' ')} UTC  {f['name']}  ({int(f.get('size', 0)):,} bytes)")
            return 0
        if not a.name.startswith(PREFIX):
            raise OffsiteError(f"Backup names must start with '{PREFIX}'")
        data = sys.stdin.buffer.read()
        info = upload(http, folder, a.name, data)
        removed = prune(http, folder, get_settings().backup_drive_keep_days)
        print(f"offsite OK: {info['name']} ({len(data):,} bytes)" + (f"; removed {len(removed)} old" if removed else ""))
        _record_success(info["name"])
        return 0
    except OffsiteError as e:
        print(f"OFFSITE FAILED: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — report anything (auth, network) as a clear failure
        print(f"OFFSITE FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
