import csv

from app.cli import seed_staff
from app.db import SessionLocal
from app.models import Staff
from app.security import hash_pin, verify_password

COLS = "staff_id,name,email,department,location_type,join_date,role"


def write(tmp_path, *lines):
    p = tmp_path / "staff.csv"
    p.write_text("\n".join([COLS] + list(lines)) + "\n")
    return p


def test_seed_creates_staff_and_credentials_file(tmp_path):
    p = write(tmp_path,
              "AB0001,Brendan,Brendan@Example.com,First Aid,onshore,2020-01-01,admin",
              "ab0010,Maria,maria@example.com,Offshore,offshore,15/03/2026,staff")
    assert seed_staff(str(p)) == 0
    creds = list(csv.DictReader((tmp_path / "staff.credentials.csv").open()))
    assert {c["staff_id"] for c in creds} == {"AB0001", "AB0010"}
    with SessionLocal() as db:
        maria = db.get(Staff, "AB0010")
        c = next(c for c in creds if c["staff_id"] == "AB0010")
        assert verify_password(maria.password_hash, c["temporary_password"])
        assert maria.pin_hash == hash_pin(c["pin"])
        assert maria.must_change_password and maria.email == "maria@example.com"
        assert str(maria.join_date) == "2026-03-15"


def test_rerun_keeps_existing_password_and_pin(tmp_path):
    p = write(tmp_path, "AB0003,Sonya,sonya@example.com,Gym,onshore,,staff")
    seed_staff(str(p))
    with SessionLocal() as db:
        before = db.get(Staff, "AB0003")
        pw, pin = before.password_hash, before.pin_hash
    p = write(tmp_path, "AB0003,Sonya Smith,sonya@example.com,Gym,onshore,,staff")
    assert seed_staff(str(p)) == 0
    with SessionLocal() as db:
        after = db.get(Staff, "AB0003")
        assert (after.name, after.password_hash, after.pin_hash) == ("Sonya Smith", pw, pin)


def test_any_bad_row_imports_nothing(tmp_path, capsys):
    p = write(tmp_path,
              "AB0003,Sonya,sonya@example.com,Gym,onshore,,staff",
              "AB0010,Maria,maria@example.com,Offshore,offshore,,staff",   # offshore needs join_date
              "AB0011,Jun,not-an-email,Offshore,offsure,2026-01-01,boss")
    assert seed_staff(str(p)) == 1
    err = capsys.readouterr().err
    assert "join_date is required" in err and "email looks wrong" in err and "location_type" in err
    with SessionLocal() as db:
        assert db.query(Staff).count() == 0


def test_duplicate_pins_rejected(tmp_path):
    p = tmp_path / "staff.csv"
    p.write_text(COLS + ",pin\n"
                 "AB0003,Sonya,sonya@example.com,Gym,onshore,,staff,482913\n"
                 "AB0004,Jo,jo@example.com,Gym,onshore,,staff,482913\n")
    assert seed_staff(str(p)) == 1
