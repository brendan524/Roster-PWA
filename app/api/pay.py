"""Admin: the fortnightly pay report, venues (for travel) and the teaching-hours tracker."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import mailer, payreport, travel
from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, get_roster_service
from app.models import ExpenseClaim, PayReport, Staff, TravelLeg, Venue
from app.roster import RosterService, RosterUnavailable

router = APIRouter(prefix="/api/admin", tags=["pay"])


def _build(db: Session, service: RosterService, end: date) -> dict:
    try:
        rep = payreport.build(db, service, end)
    except RosterUnavailable as e:
        raise HTTPException(503, str(e)) from e
    db.commit()  # new venues seen in the roster, cached distances
    return rep


def save(db: Session, rep: dict, emailed: bool) -> PayReport:
    end = date.fromisoformat(rep["period_end"])
    row = db.get(PayReport, end) or PayReport(period_end=end)
    row.period_start = date.fromisoformat(rep["period_start"])
    row.data = json.dumps(rep)
    row.generated_at = datetime.fromisoformat(rep["generated_at"])
    if emailed:
        row.emailed_at = datetime.now().astimezone()
    db.add(row)
    db.commit()
    return row


def send(db: Session, rep: dict, now: datetime | None = None) -> bool:
    s = get_settings()
    to = s.pay_report_email or s.alert_email
    start, end = date.fromisoformat(rep["period_start"]), date.fromisoformat(rep["period_end"])
    ok = mailer.queue(db, to, f"Pay report {start:%-d %b} – {end:%-d %b %Y} (pay day {date.fromisoformat(rep['pay_day']):%a %-d %b})",
                      payreport.email_text(rep))
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(s.timezone)).date()
    if end < today:  # only once the fortnight's hours are final
        ids = [i for p in rep["people"] for i in p.get("expense_ids", [])]
        if ids:  # these claims are paid with this pay run, so they don't appear again next fortnight
            db.query(ExpenseClaim).filter(ExpenseClaim.id.in_(ids), ExpenseClaim.paid_in_period.is_(None)) \
                .update({"paid_in_period": end}, synchronize_session=False)
        for t in rep.get("tracker", []):
            if t.get("reached"):
                mailer.queue(db, to, f"Level up: {t['name']} has reached {t['reached']:,} teaching hours",
                             f"{t['name']} ({t['staff_id']}) reached {t['reached']:,} teaching hours in the fortnight "
                             f"{start:%-d %b} – {end:%-d %b %Y} ({t['total']:,.1f} hours in total).\n\n"
                             f"Under the award (Schedule B, 3.3) they move up a level — now Level {t['level']}. "
                             "Please update their pay rates in Xero from the next pay period.",
                             f"level:{t['staff_id']}:{t['reached']}")
    save(db, rep, emailed=ok)
    return ok


@router.get("/pay")
def pay_report(end: date | None = None, _: Staff = Depends(admin_staff), db: Session = Depends(get_db),
               service: RosterService = Depends(get_roster_service)):
    today = service.today()
    cur_start, cur_end = payreport.period_for(today)
    end = payreport.period_for(end)[1] if end else cur_end
    if end > cur_end:
        raise HTTPException(400, "That pay period hasn't started yet.")
    rep = _build(db, service, end)
    saved = db.get(PayReport, end)
    periods = [cur_end - timedelta(days=14 * i) for i in range(7)]
    return rep | {
        "current": end == cur_end,
        "emailed_at": saved.emailed_at.isoformat() if saved and saved.emailed_at else None,
        "periods": [{"end": e.isoformat(), "start": (e - timedelta(days=13)).isoformat()} for e in periods],
        "email_to": get_settings().pay_report_email or get_settings().alert_email,
    }


@router.post("/pay/email")
def email_now(end: date, _: Staff = Depends(admin_staff), db: Session = Depends(get_db),
              service: RosterService = Depends(get_roster_service)):
    if not mailer.configured():
        raise HTTPException(400, "Email isn't set up on the server (SMTP_USER / SMTP_PASSWORD in .env).")
    s = get_settings()
    if not (s.pay_report_email or s.alert_email):
        raise HTTPException(400, "No one to send it to: add PAY_REPORT_EMAIL (or ALERT_EMAIL) to .env, "
                                 "then run docker compose up -d.")
    rep = _build(db, service, payreport.period_for(end)[1])
    if not send(db, rep):
        raise HTTPException(500, "Couldn't queue the email — try again.")
    db.commit()
    return {"ok": True}


# ---- venues ------------------------------------------------------------------------

def _venue_out(db: Session, v: Venue) -> dict:
    office = get_settings().office_address
    there = db.get(TravelLeg, (office, v.address)) if office and v.address else None
    return {"name": v.name, "label": v.label, "kind": v.kind, "address": v.address,
            "km": there.km if there else None, "minutes": there.minutes if there else None,
            "source": there.source if there else None}


@router.get("/venues")
def venues(_: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    s = get_settings()
    rows = db.query(Venue).order_by(Venue.kind != "unknown", Venue.label).all()
    return {"office_address": s.office_address, "maps": bool(s.google_maps_api_key),
            "venues": [_venue_out(db, v) for v in rows]}


class VenueIn(BaseModel):
    name: str
    kind: str = Field(pattern="^(campus|client|unknown)$")
    address: str = Field("", max_length=300)
    km: float | None = Field(None, ge=0, le=2000)  # one way from the office, typed in by hand
    minutes: float | None = Field(None, ge=0, le=1440)


@router.post("/venues")
def save_venue(body: VenueIn, _: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    v = db.get(Venue, travel.key(body.name))
    if v is None:
        raise HTTPException(404, "Unknown venue.")
    v.kind, v.address = body.kind, " ".join(body.address.split())
    office = get_settings().office_address
    if body.km is not None and body.minutes is not None:
        if not office or not v.address:
            raise HTTPException(400, "Set the office address (OFFICE_ADDRESS) and this venue's address first.")
        for a, b in ((office, v.address), (v.address, office)):
            leg = db.get(TravelLeg, (a, b)) or TravelLeg(origin=a, destination=b)
            leg.km, leg.minutes, leg.source = body.km, body.minutes, "manual"
            db.add(leg)
    db.commit()
    if v.kind == "client" and v.address and office and body.km is None:
        travel.leg(db, office, v.address)  # look it up now so the admin sees it
        db.commit()
    return _venue_out(db, v)


# ---- teaching hours ----------------------------------------------------------------

class TeachingIn(BaseModel):
    staff_id: str
    hours: float = Field(ge=0, le=100000)
    as_of: date


@router.post("/teaching-hours")
def set_teaching_hours(body: TeachingIn, _: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    st = db.get(Staff, body.staff_id)
    if st is None:
        raise HTTPException(404, "No such staff member.")
    st.teaching_hours_opening, st.teaching_hours_as_of = body.hours, body.as_of
    db.commit()
    return {"ok": True}
