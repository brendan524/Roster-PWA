import os

os.environ.setdefault("DATABASE_URL", os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://roster:roster@localhost:5433/roster_test"))
os.environ["PIN_PEPPER"] = "test-pepper"
os.environ["COOKIE_SECURE"] = "false"
os.environ["ROSTER_SOURCE"] = "csv"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.deps import get_roster_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Staff  # noqa: E402
from app.roster import RosterService  # noqa: E402
from app.security import hash_password, hash_pin  # noqa: E402

PASSWORD = "correct-horse-1"


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE auth_sessions, staff CASCADE"))
    yield


class FakeSource:
    def __init__(self, tabs=None):
        self.tabs = tabs or {}
        self.calls = 0
        self.fail = False

    def read_tabs(self, names):
        self.calls += 1
        if self.fail:
            raise ConnectionError("sheet down")
        return {n: self.tabs.get(n) for n in names}


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


def login(client, email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})
