"""Admin command line. Run inside the app container, e.g.

    docker compose exec roster-app python -m app.cli seed-staff /data/staff.csv
    docker compose exec roster-app python -m app.cli reset-password AB0003
    docker compose exec roster-app python -m app.cli set-pin AB0003
    docker compose exec roster-app python -m app.cli set-role AB0001 admin
    docker compose exec roster-app python -m app.cli list-staff

seed-staff is the ONE-TIME loader for the staff list (it's safe to re-run: it
updates existing people by Staff ID and never overwrites an existing password or
PIN unless the CSV gives a new one). After seeding, the database is the source of truth.
"""

import argparse
import csv
import getpass
import re
import sys
from pathlib import Path

from app.db import SessionLocal
from app.models import Staff
from app.roster import CellError, parse_date
from app.security import (
    delete_all_sessions,
    generate_password,
    generate_pin,
    hash_password,
    hash_pin,
    validate_new_password,
    validate_pin,
)

REQUIRED_COLUMNS = ["staff_id", "name", "location_type"]
OPTIONAL_COLUMNS = ["email", "department", "join_date", "role", "employment_status", "notes", "password", "pin"]


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def seed_staff(path: str) -> int:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        first = fh.readline()
        fh.seek(0)
        # Rows copied straight out of a spreadsheet are tab-separated.
        delimiter = "\t" if first.count("\t") > first.count(",") else ","
        reader = csv.DictReader(fh, delimiter=delimiter)
        reader.fieldnames = [_norm(h) for h in reader.fieldnames or []]
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            print(f"CSV is missing column(s): {', '.join(missing)}", file=sys.stderr)
            return 1
        rows = [r for r in reader if any((v or "").strip() for v in r.values())]

    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()
    seen_pins: set[str] = set()
    cleaned = []
    for n, r in enumerate(rows, start=2):
        v = {k: (r.get(k) or "").strip() for k in REQUIRED_COLUMNS + OPTIONAL_COLUMNS}
        v["staff_id"] = v["staff_id"].upper()
        v["email"] = v["email"].lower()
        v["location_type"] = v["location_type"].lower()
        v["role_given"] = bool(v["role"])
        v["role"] = (v["role"] or "staff").lower()

        def err(msg: str) -> None:
            errors.append(f"line {n} ({v['staff_id'] or '?'}): {msg}")

        if r.get(None):  # DictReader puts surplus fields under the None key
            err("has more columns than the header — usually a comma inside a field "
                "(wrap that field in double quotes)")

        if not re.fullmatch(r"[A-Z0-9_-]{2,32}", v["staff_id"]):
            err("staff_id must be letters/numbers, e.g. AB0001")
        if v["staff_id"] in seen_ids:
            err("duplicate staff_id")
        seen_ids.add(v["staff_id"])
        if not v["name"]:
            err("name is empty")
        if v["email"]:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v["email"]):
                err("email looks wrong")
            if v["email"] in seen_emails:
                err("duplicate email")
            seen_emails.add(v["email"])
        if v["location_type"] not in ("onshore", "offshore"):
            err("location_type must be onshore or offshore")
        if v["role"] not in ("staff", "admin"):
            err("role must be staff or admin")
        join = None
        if v["join_date"]:
            try:
                join = parse_date(v["join_date"])
            except CellError as e:
                err(f"join_date: {e}")
        elif v["location_type"] == "offshore":
            warnings.append(f"{v['staff_id']} ({v['name']}): no join_date — needed before offshore leave "
                            "accrual (Stage 3) can run for them.")
        if v["password"] and (msg := validate_new_password(v["password"])):
            err(f"password: {msg}")
        if v["pin"]:
            if msg := validate_pin(v["pin"]):
                err(f"pin: {msg}")
            if v["pin"] in seen_pins:
                err("pin is the same as another person's")
            seen_pins.add(v["pin"])
        cleaned.append((v, join))

    if errors:
        print("Nothing imported. Fix these and re-run:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 1

    issued: list[dict] = []
    with SessionLocal() as db:
        for v, join in cleaned:
            staff = db.get(Staff, v["staff_id"])
            if staff is None:
                staff = Staff(staff_id=v["staff_id"], must_change_password=True, failed_logins=0, active=True)
                db.add(staff)
            staff.name = v["name"]
            staff.email = v["email"] or None
            staff.department = v["department"]
            staff.location_type = v["location_type"]
            staff.join_date = join
            staff.employment_status = v["employment_status"]
            staff.notes = v["notes"][:500]
            # Role is only changed when the CSV has a role column value; set-role is the usual way.
            if v["role_given"]:
                staff.role = v["role"]
            elif not staff.role:
                staff.role = "staff"

            new_password = v["password"] or (None if staff.password_hash else generate_password())
            if new_password:
                staff.password_hash = hash_password(new_password)
                staff.must_change_password = True
            db.flush()

            new_pin = v["pin"] or (None if staff.pin_hash else generate_pin(db))
            if new_pin:
                pin_hash = hash_pin(new_pin)
                clash = db.query(Staff).filter(Staff.pin_hash == pin_hash, Staff.staff_id != staff.staff_id).first()
                if clash:
                    db.rollback()
                    print(f"Nothing imported: PIN for {staff.staff_id} is already used by {clash.staff_id}.",
                          file=sys.stderr)
                    return 1
                staff.pin_hash = pin_hash
                db.flush()
            if new_password or new_pin:
                issued.append({"staff_id": staff.staff_id, "name": staff.name, "login": staff.staff_id,
                               "temporary_password": new_password or "(unchanged)", "pin": new_pin or "(unchanged)"})
        db.commit()

    print(f"Imported {len(cleaned)} staff.")
    for w in warnings:
        print("  note: " + w)
    if issued:
        out = Path(path).with_suffix(".credentials.csv")
        with out.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(issued[0].keys()))
            w.writeheader()
            w.writerows(issued)
        print(f"New logins/PINs for {len(issued)} people written to {out}")
        print("Hand these out privately, then DELETE that file. Staff must change the password on first login.")
    return 0


def _get(db, staff_id: str) -> Staff:
    staff = db.get(Staff, staff_id.upper())
    if staff is None:
        sys.exit(f"No staff member with ID {staff_id}")
    return staff


def reset_password(staff_id: str, choose: bool) -> int:
    with SessionLocal() as db:
        staff = _get(db, staff_id)
        if choose:
            pw = getpass.getpass("New password: ")
            if msg := validate_new_password(pw):
                sys.exit(msg)
        else:
            pw = generate_password()
        staff.password_hash = hash_password(pw)
        staff.must_change_password = True
        staff.failed_logins = 0
        staff.locked_until = None
        db.commit()
        delete_all_sessions(db, staff.staff_id)
        print(f"{staff.name} ({staff.staff_id}) has been logged out everywhere.")
        if not choose:
            print(f"Temporary password: {pw}")
        print("They'll be asked to set a new password when they log in.")
    return 0


def set_pin(staff_id: str, choose: bool) -> int:
    with SessionLocal() as db:
        staff = _get(db, staff_id)
        if choose:
            pin = getpass.getpass("New PIN: ")
            if msg := validate_pin(pin):
                sys.exit(msg)
            clash = db.query(Staff).filter(Staff.pin_hash == hash_pin(pin), Staff.staff_id != staff.staff_id).first()
            if clash:
                sys.exit("That PIN is already in use. Choose another.")
        else:
            pin = generate_pin(db)
        staff.pin_hash = hash_pin(pin)
        db.commit()
        print(f"PIN for {staff.name} ({staff.staff_id}) updated." + ("" if choose else f" New PIN: {pin}"))
    return 0


def set_active(staff_id: str, active: bool) -> int:
    with SessionLocal() as db:
        staff = _get(db, staff_id)
        staff.active = active
        db.commit()
        if not active:
            delete_all_sessions(db, staff.staff_id)
        print(f"{staff.name} ({staff.staff_id}) is now {'active' if active else 'inactive'}.")
    return 0


def set_role(staff_id: str, role: str) -> int:
    with SessionLocal() as db:
        staff = _get(db, staff_id)
        staff.role = role
        db.commit()
        print(f"{staff.name} ({staff.staff_id}) is now {role}.")
    return 0


def list_staff() -> int:
    with SessionLocal() as db:
        for s in db.query(Staff).order_by(Staff.staff_id):
            flags = [s.role, s.location_type] + ([] if s.active else ["INACTIVE"])
            print(f"{s.staff_id:<8} {s.name:<22} {s.email or '-':<28} {s.department:<20} {' '.join(flags)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-staff", help="load/update staff from a CSV").add_argument("csv")
    for name in ("reset-password", "set-pin"):
        sp = sub.add_parser(name)
        sp.add_argument("staff_id")
        sp.add_argument("--choose", action="store_true", help="type the value instead of generating one")
    sub.add_parser("deactivate").add_argument("staff_id")
    sub.add_parser("activate").add_argument("staff_id")
    sr = sub.add_parser("set-role", help="make someone an admin, or back to staff")
    sr.add_argument("staff_id")
    sr.add_argument("role", choices=["admin", "staff"])
    sub.add_parser("list-staff")
    a = p.parse_args(argv)

    if a.cmd == "seed-staff":
        return seed_staff(a.csv)
    if a.cmd == "reset-password":
        return reset_password(a.staff_id, a.choose)
    if a.cmd == "set-pin":
        return set_pin(a.staff_id, a.choose)
    if a.cmd == "set-role":
        return set_role(a.staff_id, a.role)
    if a.cmd in ("activate", "deactivate"):
        return set_active(a.staff_id, a.cmd == "activate")
    return list_staff()


if __name__ == "__main__":
    sys.exit(main())
