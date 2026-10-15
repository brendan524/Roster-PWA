"""Clock in/out: the pay-critical paths. Run against real Postgres."""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal, engine
from app.main import app
from app.models import Kiosk, Punch, Staff
from app.punches import analyse, pair_sessions, utcnow
from app.roster import parse_tab
from tests.conftest import HEADER, PASSWORD, add_staff, login

MEL = ZoneInfo("Australia/Melbourne")
PIN = "482913"


def cid():
    return str(uuid.uuid4())


def ms(dt: datetime) -> float:
    return dt.timestamp() * 1000


def punch_body(kind="in", pin=PIN, **kw):
    body = {"client_id": cid(), "kind": kind, "pin": pin, "device_time": ms(utcnow()), "sent_at": ms(utcnow()),
            "queued": False, "geo": {"status": "ok", "lat": -37.7, "lon": 144.9, "accuracy": 12}}
    body.update(kw)
    return body


def punches_for(staff_id):
    with SessionLocal() as db:
        return db.query(Punch).filter_by(staff_id=staff_id).order_by(Punch.occurred_at).all()


@pytest.fixture
def sonya(client, source):
    add_staff("AB0010", None, pin=PIN)
    login(client, "AB0010")
    return client


# ---- personal device ----------------------------------------------------------------

def test_clock_in_and_out(sonya):
    r = sonya.post("/api/clock/punch", json=punch_body("in"))
    assert r.status_code == 200 and r.json()["clocked_in"] is True and r.json()["duplicate"] is False
    me = sonya.get("/api/clock/me").json()
    assert me["clocked_in"] and me["since"] and me["recent"][0]["kind"] == "in"
    r = sonya.post("/api/clock/punch", json=punch_body("out"))
    assert r.json()["clocked_in"] is False
    [p_in, p_out] = punches_for("AB0010")
    assert (p_in.kind, p_out.kind) == ("in", "out")
    assert p_in.lat == -37.7 and p_in.geo_status == "ok" and p_in.source == "personal"


def test_wrong_pin_then_lockout(sonya):
    for _ in range(5):
        assert sonya.post("/api/clock/punch", json=punch_body(pin="000000")).status_code == 401
    assert sonya.post("/api/clock/punch", json=punch_body()).status_code == 429
    assert punches_for("AB0010") == []


def test_someone_elses_pin_is_rejected_on_a_personal_device(sonya):
    add_staff("AB0011", None, pin="771234")
    assert sonya.post("/api/clock/punch", json=punch_body(pin="771234")).status_code == 401
    assert punches_for("AB0011") == [] and punches_for("AB0010") == []


def test_no_pin_set(client, source):
    add_staff("AB0012", None)
    login(client, "AB0012")
    r = client.post("/api/clock/punch", json=punch_body())
    assert r.status_code == 400 and "PIN" in r.json()["detail"]


def test_resending_the_same_punch_stores_it_once(sonya):
    body = punch_body()
    first = sonya.post("/api/clock/punch", json=body).json()
    again = sonya.post("/api/clock/punch", json=body | {"queued": True}).json()
    assert again["duplicate"] is True and again["occurred_at"] == first["occurred_at"]
    assert len(punches_for("AB0010")) == 1


def test_double_tap_counts_once(sonya):
    sonya.post("/api/clock/punch", json=punch_body())
    r = sonya.post("/api/clock/punch", json=punch_body())  # new client_id, seconds later
    assert r.json()["duplicate"] is True
    assert len(punches_for("AB0010")) == 1


def test_clocking_in_twice_far_apart_keeps_both(sonya):
    real = utcnow() - timedelta(hours=3)
    sonya.post("/api/clock/punch", json=punch_body(device_time=ms(real), sent_at=ms(utcnow()), queued=True))
    sonya.post("/api/clock/punch", json=punch_body())
    assert len(punches_for("AB0010")) == 2  # kept; the first shows as "no clock-out" for an admin


def test_queued_punch_corrects_a_wrong_phone_clock(sonya):
    # Phone clock is 1 hour fast. The punch happened 40 min ago and is only now sent.
    skew = timedelta(hours=1)
    really_happened = utcnow() - timedelta(minutes=40)
    body = punch_body(device_time=ms(really_happened + skew), sent_at=ms(utcnow() + skew), queued=True)
    sonya.post("/api/clock/punch", json=body)
    [p] = punches_for("AB0010")
    assert abs((p.occurred_at - really_happened).total_seconds()) < 5
    assert p.queued and abs(p.clock_offset_s + 3600) < 5


def test_live_punch_uses_server_time_not_phone_time(sonya):
    sonya.post("/api/clock/punch", json=punch_body(device_time=ms(utcnow() - timedelta(hours=5)),
                                                   sent_at=ms(utcnow() - timedelta(hours=5))))
    [p] = punches_for("AB0010")
    assert abs((p.occurred_at - utcnow()).total_seconds()) < 10


def test_queued_punch_can_never_be_in_the_future(sonya):
    sonya.post("/api/clock/punch", json=punch_body(device_time=ms(utcnow() + timedelta(hours=2)),
                                                   sent_at=ms(utcnow()), queued=True))
    [p] = punches_for("AB0010")
    assert p.occurred_at <= utcnow()


def test_missing_gps_never_blocks_a_punch(sonya):
    r = sonya.post("/api/clock/punch", json=punch_body(geo={"status": "denied", "lat": 1, "lon": 2}))
    assert r.status_code == 200
    [p] = punches_for("AB0010")
    assert p.geo_status == "denied" and p.lat is None and p.lon is None


def test_must_be_logged_in(client, source):
    assert client.post("/api/clock/punch", json=punch_body()).status_code == 401


# ---- kiosk -------------------------------------------------------------------------

def make_kiosk(client, location="AB HQ"):
    add_staff("AB0001", None, role="admin")
    r = client.post("/api/kiosk/setup", json={"login": "AB0001", "password": PASSWORD, "location": location})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def kiosk_client(token):
    return TestClient(app, headers={"x-ab-app": "1", "x-kiosk-token": token})


def test_kiosk_setup_needs_an_admin(client, source):
    add_staff("AB0010", None, pin=PIN)
    r = client.post("/api/kiosk/setup", json={"login": "AB0010", "password": PASSWORD, "location": "AB HQ"})
    assert r.status_code == 403
    add_staff("AB0001", None, role="admin")
    r = client.post("/api/kiosk/setup", json={"login": "AB0001", "password": "wrong-pass", "location": "AB HQ"})
    assert r.status_code == 401
    r = client.post("/api/kiosk/setup", json={"login": "AB0001", "password": PASSWORD, "location": "Mars"})
    assert r.status_code == 400


def test_kiosk_punch_identifies_person_by_pin(client, source):
    token = make_kiosk(client, "WrkPod")
    add_staff("AB0003", None, pin="551177")
    with kiosk_client(token) as k:
        me = k.get("/api/kiosk/me").json()
        assert me["name"] == "WrkPod" and me["tz"] == "Asia/Manila"
        r = k.post("/api/kiosk/punch", json=punch_body(pin="551177", geo={"status": "unavailable"}))
        assert r.status_code == 200 and r.json()["name"] == "Ab0003" and r.json()["clocked_in"]
    [p] = punches_for("AB0003")
    assert p.source == "kiosk" and p.kiosk_id is not None and p.geo_status == "kiosk"


def test_kiosk_rejects_bad_tokens_and_removed_kiosks(client, source):
    token = make_kiosk(client)
    with kiosk_client("not-a-token") as k:
        assert k.get("/api/kiosk/me").status_code == 401
    with SessionLocal() as db:
        db.query(Kiosk).update({"active": False})
        db.commit()
    with kiosk_client(token) as k:
        assert k.get("/api/kiosk/me").status_code == 401


def test_kiosk_pauses_after_repeated_wrong_pins(client, source):
    token = make_kiosk(client)
    add_staff("AB0003", None, pin="551177")
    with kiosk_client(token) as k:
        for _ in range(5):
            assert k.post("/api/kiosk/punch", json=punch_body(pin="123987")).status_code == 401
        assert k.post("/api/kiosk/punch", json=punch_body(pin="551177")).status_code == 429


def test_inactive_staff_cannot_clock_in_at_kiosk(client, source):
    token = make_kiosk(client)
    add_staff("AB0003", None, pin="551177", active=False)
    with kiosk_client(token) as k:
        assert k.post("/api/kiosk/punch", json=punch_body(pin="551177")).status_code == 401


def test_kiosk_needs_the_app_header(client, source):
    token = make_kiosk(client)
    add_staff("AB0003", None, pin="551177")
    with TestClient(app, headers={"x-kiosk-token": token}) as k:
        assert k.post("/api/kiosk/punch", json=punch_body(pin="551177")).status_code == 403


# ---- concurrency -------------------------------------------------------------------

def _parallel(n, fn):
    """Run fn(0..n-1) on n threads released at the same instant, to provoke races."""
    errors = []
    gate = threading.Barrier(n)

    def run(i):
        try:
            gate.wait()
            fn(i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_concurrent_kiosk_use_by_many_people(client, source):
    token = make_kiosk(client)
    pins = {f"AB{i:04d}": f"{700000 + i * 37}" for i in range(20, 40)}
    for sid, pin in pins.items():
        add_staff(sid, None, pin=pin)
    items = list(pins.items())
    statuses = []

    def go(i):
        with kiosk_client(token) as k:
            statuses.append(k.post("/api/kiosk/punch", json=punch_body(pin=items[i][1])).status_code)

    _parallel(len(items), go)
    assert statuses == [200] * len(items)
    for sid in pins:
        assert len(punches_for(sid)) == 1


def test_concurrent_resends_of_one_punch_store_it_once(client, source):
    token = make_kiosk(client)
    add_staff("AB0003", None, pin="551177")
    body = punch_body(pin="551177")
    clients = [kiosk_client(token) for _ in range(12)]
    _parallel(12, lambda i: clients[i].post("/api/kiosk/punch", json=body))
    assert len(punches_for("AB0003")) == 1


def test_concurrent_double_taps_store_one_punch(client, source):
    token = make_kiosk(client)
    add_staff("AB0003", None, pin="551177")
    clients = [kiosk_client(token) for _ in range(12)]
    _parallel(12, lambda i: clients[i].post("/api/kiosk/punch", json=punch_body(pin="551177")))
    assert len(punches_for("AB0003")) == 1


# ---- audit trail ---------------------------------------------------------------------

def test_punches_cannot_be_edited_or_deleted_even_directly(sonya):
    sonya.post("/api/clock/punch", json=punch_body())
    for sql in ("UPDATE punches SET kind = 'out'", "DELETE FROM punches"):
        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text(sql))
    assert len(punches_for("AB0010")) == 1


def test_admin_corrections_are_recorded_with_reason(client, source):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None, pin=PIN)
    login(client, "AB0001")
    today = utcnow().astimezone(MEL).date()
    r = client.post("/api/admin/clock/punch", json={"staff_id": "AB0010", "kind": "in",
                                                    "local_time": f"{today}T00:05", "reason": "Forgot to clock in"})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.post("/api/admin/clock/punch", json={"staff_id": "AB0010", "kind": "out",
                                                       "local_time": f"{today}T00:06", "reason": ""}).status_code == 422
    assert client.post("/api/admin/clock/void", json={"punch_id": pid, "reason": "Entered by mistake"}).status_code == 200
    assert client.post("/api/admin/clock/void", json={"punch_id": pid, "reason": "again"}).status_code == 409
    [p] = punches_for("AB0010")
    assert p.source == "admin" and p.created_by == "AB0001" and p.reason == "Forgot to clock in"
    day = client.get(f"/api/admin/clock?day={today}").json()
    assert day["sessions"] == []  # the voided punch no longer counts


def test_admin_cannot_enter_future_times(client, source):
    add_staff("AB0001", None, role="admin")
    login(client, "AB0001")
    later = (utcnow() + timedelta(hours=3)).astimezone(MEL).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/api/admin/clock/punch", json={"staff_id": "AB0001", "kind": "in", "local_time": later,
                                                    "reason": "test"})
    assert r.status_code == 400


def test_admin_day_view(client, source):
    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None, pin=PIN)
    today = utcnow().astimezone(MEL).date()
    source.tabs = {f"{today:%Y-%m}": [HEADER, [today.isoformat(), "AB0010", "Sonya", "Fitness", "Cardio",
                                                "00:00", "23:59", "AB HQ", "", "", ""]]}
    login(client, "AB0010")
    client.post("/api/clock/punch", json=punch_body())
    client.post("/api/auth/logout")
    login(client, "AB0001")
    day = client.get("/api/admin/clock").json()
    assert day["roster_known"] and [s["staff_id"] for s in day["sessions"]] == ["AB0010"]
    assert [o["staff_id"] for o in day["on_clock"]] == ["AB0010"]
    codes = [f["code"] for f in day["sessions"][0]["flags"]]
    assert "on_clock" in codes and "unrostered" not in codes
    client.post("/api/auth/logout")
    login(client, "AB0010")
    assert client.get("/api/admin/clock").status_code == 403


# ---- matching and flags (no database needed) -----------------------------------------

def P(kind, when, sid="AB0010", **kw):
    return Punch(id=abs(hash((kind, when))) % 10**9, staff_id=sid, kind=kind, occurred_at=when, source=kw.get("source", "kiosk"),
                 geo_status=kw.get("geo_status", "kiosk"), queued=kw.get("queued", False),
                 received_at=kw.get("received_at", when), clock_offset_s=kw.get("offset"))


def shifts_for(rows, tab="2026-10"):
    s, issues = parse_tab(tab, [HEADER] + rows)
    assert issues == []
    return s


def mel(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=MEL).astimezone(UTC)


def row(d, start, end, sid="AB0010"):
    return [d, sid, "Sonya", "Fitness", "Cardio", start, end, "AB HQ", "", "", ""]


def codes(sess):
    return [f["code"] for f in sess.flags]


def test_grace_period_is_10_minutes():
    sh = shifts_for([row("2026-10-20", "09:00", "17:00")])
    on_time = pair_sessions([P("in", mel(2026, 10, 20, 9, 10)), P("out", mel(2026, 10, 20, 16, 50))])
    analyse(on_time, sh, MEL, mel(2026, 10, 21, 9))
    assert codes(on_time[0]) == []
    late = pair_sessions([P("in", mel(2026, 10, 20, 9, 11)), P("out", mel(2026, 10, 20, 16, 49))])
    analyse(late, shifts_for([row("2026-10-20", "09:00", "17:00")]), MEL, mel(2026, 10, 21, 9))
    assert codes(late[0]) == ["late", "early_finish"]
    assert late[0].flags[0]["minutes"] == 11 and late[0].worked_hours == 7.63


def test_forgotten_clock_out_is_flagged_after_shift_end():
    sh = shifts_for([row("2026-10-20", "09:00", "17:00")])
    sess = pair_sessions([P("in", mel(2026, 10, 20, 9))])
    analyse(sess, sh, MEL, mel(2026, 10, 20, 15))
    assert codes(sess[0]) == ["on_clock"]
    sess = pair_sessions([P("in", mel(2026, 10, 20, 9))])
    analyse(sess, shifts_for([row("2026-10-20", "09:00", "17:00")]), MEL, mel(2026, 10, 20, 19, 1))
    assert codes(sess[0]) == ["no_clock_out"]


def test_odd_sequences_are_kept_and_flagged():
    sessions = pair_sessions([P("in", mel(2026, 10, 20, 9)), P("in", mel(2026, 10, 21, 9)),
                              P("out", mel(2026, 10, 21, 17)), P("out", mel(2026, 10, 22, 17))])
    assert [(s.clock_in is not None, s.clock_out is not None) for s in sessions] == [
        (True, False), (True, True), (False, True)]
    analyse(sessions, [], MEL, mel(2026, 10, 23, 9))
    assert "no_clock_out" in codes(sessions[0]) and "no_clock_in" in codes(sessions[2])
    assert all("unrostered" in codes(s) for s in sessions)


def test_no_shows_and_unknown_roster():
    sh = shifts_for([row("2026-10-20", "09:00", "17:00"), row("2026-10-20", "18:00", "19:00")])
    no_shows = analyse([], sh, MEL, mel(2026, 10, 20, 9, 30))
    assert [s.start for s in no_shows] == ["09:00"]  # the 18:00 one hasn't started yet
    sess = pair_sessions([P("in", mel(2026, 9, 1, 9))])
    analyse(sess, [], MEL, mel(2026, 9, 1, 10), roster_known=False)
    assert "unrostered" not in codes(sess[0])  # we can't know, so we don't accuse


def test_back_to_back_shifts_match_the_right_one():
    sh = shifts_for([row("2026-10-20", "07:30", "09:00"), row("2026-10-20", "09:30", "10:30")])
    sessions = pair_sessions([P("in", mel(2026, 10, 20, 7, 28)), P("out", mel(2026, 10, 20, 9, 2)),
                              P("in", mel(2026, 10, 20, 9, 29)), P("out", mel(2026, 10, 20, 10, 31))])
    analyse(sessions, sh, MEL, mel(2026, 10, 21, 0))
    assert [s.shift.start for s in sessions] == ["07:30", "09:30"]
    assert all(codes(s) == [] for s in sessions)


def test_daylight_saving_change_day():
    # Melbourne clocks went forward at 2am on 4 Oct 2026. A 9am shift that day is 22:00 UTC on 3 Oct.
    sh = shifts_for([row("2026-10-04", "09:00", "12:00")])
    sess = pair_sessions([P("in", datetime(2026, 10, 3, 22, 5, tzinfo=UTC)),
                          P("out", datetime(2026, 10, 4, 1, 0, tzinfo=UTC))])
    analyse(sess, sh, MEL, mel(2026, 10, 5, 9))
    assert codes(sess[0]) == [] and sess[0].worked_hours == 2.92


def test_offshore_shift_matched_by_the_same_instant():
    # Dan rostered 09:00 Melbourne; clocks in at 06:02 Philippines time on the kiosk.
    sh = shifts_for([row("2026-10-20", "09:00", "18:00", sid="AB0003")])
    ph = ZoneInfo("Asia/Manila")
    sess = pair_sessions([P("in", datetime(2026, 10, 20, 6, 2, tzinfo=ph).astimezone(UTC), sid="AB0003")])
    analyse(sess, sh, MEL, mel(2026, 10, 20, 12))
    assert sess[0].shift is not None and codes(sess[0]) == ["on_clock"]


def test_offline_and_gps_flags():
    sh = shifts_for([row("2026-10-20", "09:00", "17:00")])
    t = mel(2026, 10, 20, 9)
    sess = pair_sessions([P("in", t, source="personal", geo_status="denied", queued=True, offset=-600,
                            received_at=t + timedelta(minutes=30))])
    analyse(sess, sh, MEL, mel(2026, 10, 20, 12))
    assert set(codes(sess[0])) == {"no_gps", "offline", "clock_adjusted", "on_clock"}


def test_in_out_in_within_a_minute_keeps_all_three(sonya):
    """Not a double-tap: an opposite punch in between means it's a real new action."""
    for kind in ("in", "out", "in"):
        r = sonya.post("/api/clock/punch", json=punch_body(kind))
        assert r.json()["duplicate"] is False, kind
    assert [p.kind for p in punches_for("AB0010")] == ["in", "out", "in"]
    assert sonya.get("/api/clock/me").json()["clocked_in"] is True


def test_auto_clock_at_rostered_times(client, source):
    from app import autoclock
    from app.deps import get_roster_service
    from app.models import PunchVoid

    add_staff("AB0001", None, role="admin")
    add_staff("AB0010", None)  # clocks normally: never auto-punched
    with SessionLocal() as db:
        db.get(Staff, "AB0001").auto_clock = True
        db.commit()
    today = utcnow().astimezone(MEL).date()
    source.tabs = {f"{today:%Y-%m}": [HEADER,
                                      [today.isoformat(), "AB0001", "Brendan", "Admin", "Office", "09:00", "17:00", "AB HQ", "", "", ""],
                                      [today.isoformat(), "AB0010", "Sonya", "Fitness", "Cardio", "09:00", "17:00", "AB HQ", "", "", ""]]}
    service = app.dependency_overrides[get_roster_service]()
    start = datetime.combine(today, datetime.min.time().replace(hour=9), MEL).astimezone(UTC)
    end = start + timedelta(hours=8)
    with SessionLocal() as db:
        assert autoclock.run(db, service, now=start - timedelta(minutes=1)) == 0  # not started yet
        assert autoclock.run(db, service, now=start + timedelta(hours=1)) == 1  # clocked in at 9:00
        assert autoclock.run(db, service, now=end + timedelta(minutes=5)) == 1  # clocked out at 17:00
        assert autoclock.run(db, service, now=end + timedelta(minutes=10)) == 0  # never doubled
    ps = punches_for("AB0001")
    assert [(p.kind, p.source, p.occurred_at) for p in ps] == [("in", "auto", start), ("out", "auto", end)]
    assert punches_for("AB0010") == []

    # An admin removing an automatic punch sticks: the job doesn't put it back.
    with SessionLocal() as db:
        db.add(PunchVoid(punch_id=ps[1].id, voided_by="AB0001", reason="left early"))
        db.commit()
        assert autoclock.run(db, service, now=end + timedelta(minutes=15)) == 0

    # The admin day view shows it as automatic, on time, no warnings.
    login(client, "AB0001")
    day = client.get(f"/api/admin/clock?day={today.isoformat()}").json()
    s = next(x for x in day["sessions"] if x["staff_id"] == "AB0001")
    assert {f["code"] for f in s["flags"]} <= {"auto", "on_clock", "no_clock_out"}
