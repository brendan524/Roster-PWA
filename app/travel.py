"""Travel for First Aid courses at client sites: driving km and time.

Each roster Location is a venue an admin marks as a permanent campus (gear kept
there; no travel paid) or a client site (travel paid from the office). A day's
paid travel is the drive office → first client site → … → last client site →
office. Distances come from the Google Maps Routes API when a key is set, and
are cached; an admin can also type a distance in by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TravelLeg, Venue

log = logging.getLogger(__name__)

MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"


def key(location: str) -> str:
    return " ".join((location or "").lower().split())


def is_campus(label: str) -> bool:
    text = (label or "").upper()
    return any(p.strip() and p.strip().upper() in text for p in get_settings().campus_patterns.split(","))


def venues_for(db: Session, locations: set[str]) -> dict[str, Venue]:
    """Venue rows for these roster locations, adding any new ones. Names matching the campus
    patterns (Tullamarine, Melton…) are campuses automatically; others wait for an admin."""
    rows = {v.name: v for v in db.query(Venue).filter(Venue.name.in_({key(l) for l in locations if key(l)}))}
    for loc in locations:
        k = key(loc)
        if k and k not in rows:
            rows[k] = Venue(name=k, label=loc.strip(), kind="unknown", address="")
            db.add(rows[k])
        if k and rows[k].kind == "unknown" and is_campus(loc):
            rows[k].kind = "campus"
    db.flush()
    return rows


def _google(origins: list[str], destinations: list[str], http=None) -> dict[tuple[int, int], tuple[float, float]]:
    s = get_settings()
    http = http or requests
    r = http.post(MATRIX_URL, timeout=30, json={
        "origins": [{"waypoint": {"address": o}} for o in origins],
        "destinations": [{"waypoint": {"address": d}} for d in destinations],
        "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE", "regionCode": "AU",
    }, headers={"X-Goog-Api-Key": s.google_maps_api_key,
                "X-Goog-FieldMask": "originIndex,destinationIndex,distanceMeters,duration,condition"})
    if r.status_code != 200:
        raise RuntimeError(f"Google Maps error {r.status_code}: {r.text[:200]}")
    out = {}
    for e in r.json():
        if e.get("condition") == "ROUTE_EXISTS":
            secs = float(str(e.get("duration", "0s")).rstrip("s") or 0)
            out[(e.get("originIndex", 0), e.get("destinationIndex", 0))] = (e.get("distanceMeters", 0) / 1000, secs / 60)
    return out


def leg(db: Session, origin: str, destination: str, http=None) -> TravelLeg | None:
    """Driving km/minutes between two addresses (cached). None if unknown and it can't be looked up."""
    if not origin or not destination:
        return None
    if origin == destination:
        return TravelLeg(origin=origin, destination=destination, km=0, minutes=0, source="manual")
    row = db.get(TravelLeg, (origin, destination))
    if row is not None:
        return row
    if not get_settings().google_maps_api_key:
        return None
    try:
        got = _google([origin], [destination], http).get((0, 0))
    except Exception:  # noqa: BLE001 — shown in the report as "distance unknown"
        log.exception("Google Maps lookup failed")
        return None
    if got is None:
        return None
    row = TravelLeg(origin=origin, destination=destination, km=round(got[0], 1), minutes=round(got[1], 1))
    db.add(row)
    db.flush()
    return row


@dataclass
class DayTravel:
    km: float | None  # None = couldn't be worked out
    minutes: float | None
    stops: list[str]
    problem: str = ""


def day_travel(db: Session, client_labels: list[str], venues: dict[str, Venue], http=None) -> DayTravel:
    """Office → each client site in order → office."""
    office = get_settings().office_address
    if not client_labels:
        return DayTravel(0, 0, [])
    if not office:
        return DayTravel(None, None, client_labels, "Office address isn't set (OFFICE_ADDRESS)")
    addresses = []
    for label in client_labels:
        v = venues.get(key(label))
        if v is None or not v.address:
            return DayTravel(None, None, client_labels, f"No address for {label}")
        if not addresses or addresses[-1] != v.address:
            addresses.append(v.address)
    route = [office, *addresses, office]
    km = minutes = 0.0
    for a, b in zip(route, route[1:]):
        lg = leg(db, a, b, http)
        if lg is None:
            return DayTravel(None, None, client_labels, f"Distance unknown for {a} → {b}")
        km += lg.km
        minutes += lg.minutes
    return DayTravel(round(km, 1), round(minutes), client_labels)
