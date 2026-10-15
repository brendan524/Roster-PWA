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
              "AB0010,Maria,maria@example.com,Offshore,offshore,31/02/2026,staff",
              "AB0011,Jun,not-an-email,Offshore,offsure,2026-01-01,boss")
    assert seed_staff(str(p)) == 1
    err = capsys.readouterr().err
    for needle in ("join_date", "email looks wrong", "location_type", "role must be"):
        assert needle in err
    with SessionLocal() as db:
        assert db.query(Staff).count() == 0


def test_duplicate_pins_rejected(tmp_path):
    p = tmp_path / "staff.csv"
    p.write_text(COLS + ",pin\n"
                 "AB0003,Sonya,sonya@example.com,Gym,onshore,,staff,482913\n"
                 "AB0004,Jo,jo@example.com,Gym,onshore,,staff,482913\n")
    assert seed_staff(str(p)) == 1


REAL_FORMAT = '''Staff ID,Name,Department,Location Type,Employment Status,Join Date,PIN,Notes
AB0001,Brendan Daynes,Fitness & First Aid,Onshore,Director,,,Director; teaches both
AB0003,Dannylloyd Tubo,Offshore,Offshore,Contractor,,,"Known as ""Dan"""
AB0010,Sonya Wilkinson,Fitness,Onshore,Casual,,,Fitness instructor
'''


def test_seed_accepts_the_real_staff_file_format(tmp_path, capsys):
    p = tmp_path / "AB-Staff-Seed.csv"
    p.write_text(REAL_FORMAT)
    assert seed_staff(str(p)) == 0
    out = capsys.readouterr().out
    assert "AB0003 (Dannylloyd Tubo): no join_date" in out  # warned, not blocked
    with SessionLocal() as db:
        dan = db.get(Staff, "AB0003")
        assert (dan.location_type, dan.employment_status, dan.email, dan.role) == ("offshore", "Contractor", None, "staff")
        assert dan.notes == 'Known as "Dan"'
    creds = list(csv.DictReader((tmp_path / "AB-Staff-Seed.credentials.csv").open()))
    assert [c["login"] for c in creds] == ["AB0001", "AB0003", "AB0010"]


def test_reseed_without_role_column_keeps_admin(tmp_path):
    from app.cli import set_role

    p = tmp_path / "s.csv"
    p.write_text(REAL_FORMAT)
    seed_staff(str(p))
    set_role("AB0001", "admin")
    seed_staff(str(p))
    with SessionLocal() as db:
        assert db.get(Staff, "AB0001").role == "admin"


def test_row_with_unquoted_comma_is_rejected(tmp_path, capsys):
    p = tmp_path / "s.csv"
    p.write_text("Staff ID,Name,Location Type,Notes\n"
                 "AB0016,Jody Wilms,Onshore,First Aid instructor (public bookings, AB HQ)\n")
    assert seed_staff(str(p)) == 1
    assert "more columns than the header" in capsys.readouterr().err


def test_tab_separated_paste_from_a_spreadsheet(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("Staff ID\tName\tLocation Type\tNotes\n"
                 "AB0016\tJody Wilms\tOnshore\tFirst Aid instructor (public bookings, AB HQ)\n")
    assert seed_staff(str(p)) == 0
    with SessionLocal() as db:
        assert db.get(Staff, "AB0016").notes == "First Aid instructor (public bookings, AB HQ)"


def test_onshore_contractors_seeded_without_leave_and_set_leave(tmp_path):
    from app.cli import main
    p = tmp_path / "staff.csv"
    p.write_text("staff_id,name,location_type,employment_status\n"
                 "AB0020,Con Tractor,onshore,Contract\nAB0021,Off Shore,offshore,Contractor\nAB0022,Cas Ual,onshore,Casual\n")
    assert seed_staff(str(p)) == 0
    with SessionLocal() as db:
        assert [db.get(Staff, i).has_leave for i in ("AB0020", "AB0021", "AB0022")] == [False, True, True]
    assert main(["set-leave", "AB0022", "no"]) == 0
    assert seed_staff(str(p)) == 0  # re-seeding keeps the admin's choice
    with SessionLocal() as db:
        assert db.get(Staff, "AB0022").has_leave is False
