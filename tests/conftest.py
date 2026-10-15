import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://roster:roster@localhost:5433/roster_test"))
os.environ["PIN_PEPPER"] = "test-pepper"
os.environ["COOKIE_SECURE"] = "false"
os.environ["ROSTER_SOURCE"] = "csv"
os.environ["AUTO_CLOCK_MINUTES"] = "0"  # tests run the jobs directly
os.environ["JOBS_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.deps import get_roster_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Staff  # noqa: E402
from app.roster import RosterService  # noqa: E402
from app.security import hash_password, hash_pin  # noqa: E402

PASSWORD = "correct-horse-1"


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Build the test schema from the real Alembic migrations, so they're tested too."""
    from alembic import command
    from alembic.config import Config

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(Config(str(Path(__file__).resolve().parent.parent / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def clean_db(migrated_db):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE auth_sessions, staff, xero_connection, email_outbox, app_state, venues, travel_legs, pay_reports CASCADE"))
    yield


class FakeSource:
    """In-memory roster sheet: {tab title: rows}."""

    def __init__(self, tabs=None):
        self.tabs = tabs or {}
        self.calls = 0
        self.fail = False

    def tab_titles(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("sheet down")
        return [t for t, rows in self.tabs.items() if rows is not None]

    def read_tabs(self, titles):
        return {t: self.tabs[t] for t in titles}


HEADER = ["Date", "Staff ID", "Staff Name", "Department", "Class / Role", "Shift Start", "Shift End",
          "Location", "Scheduled Hours", "Teaching Hours", "Notes"]


@pytest.fixture
def source(monkeypatch):
    src = FakeSource()
    service = RosterService(src, "Australia/Melbourne", ttl_seconds=120, min_refresh_seconds=0)
    app.dependency_overrides[get_roster_service] = lambda: service
    yield src
    app.dependency_overrides.clear()


def add_staff(staff_id, email, role="staff", must_change=False, active=True, pin=None):
    with SessionLocal() as db:
        db.add(Staff(staff_id=staff_id, name=staff_id.title(), email=email, department="Gym",
                     location_type="onshore", role=role, active=active, failed_logins=0,
                     password_hash=hash_password(PASSWORD), must_change_password=must_change,
                     pin_hash=hash_pin(pin) if pin else None))
        db.commit()


@pytest.fixture
def client():
    with TestClient(app, headers={"x-ab-app": "1"}) as c:
        yield c


def login(client, ident, password=PASSWORD):
    return client.post("/api/auth/login", json={"login": ident, "password": password})
