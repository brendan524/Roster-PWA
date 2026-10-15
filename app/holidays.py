"""Victorian public holidays.

Fixed rules are worked out here. The Friday before the AFL Grand Final moves each
year and is only announced once the Grand Final date is set, so it's taken from
the GRAND_FINAL_FRIDAYS setting (comma-separated dates) — plus any one-off
holidays in EXTRA_PUBLIC_HOLIDAYS.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from app.config import get_settings


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


@lru_cache(maxsize=32)
def _fixed(year: int) -> dict[date, str]:
    out: dict[date, str] = {}
    ny = date(year, 1, 1)
    out[ny] = "New Year's Day"
    if ny.weekday() >= 5:
        out[ny + timedelta(days=7 - ny.weekday())] = "New Year's Day (additional)"
    aus = date(year, 1, 26)
    out[aus if aus.weekday() < 5 else aus + timedelta(days=7 - aus.weekday())] = "Australia Day"
    out[nth_weekday(year, 3, 0, 2)] = "Labour Day"
    easter = easter_sunday(year)
    out[easter - timedelta(days=2)] = "Good Friday"
    out[easter - timedelta(days=1)] = "Easter Saturday"
    out[easter] = "Easter Sunday"
    out[easter + timedelta(days=1)] = "Easter Monday"
    out[date(year, 4, 25)] = "ANZAC Day"
    out[nth_weekday(year, 6, 0, 2)] = "King's Birthday"
    out[nth_weekday(year, 11, 1, 1)] = "Melbourne Cup Day"
    xmas, boxing = date(year, 12, 25), date(year, 12, 26)
    out[xmas] = "Christmas Day"
    out[boxing] = "Boxing Day"
    if xmas.weekday() == 5:  # Sat: Mon 27 for Christmas, Tue 28 for Boxing Day (Sun)
        out[date(year, 12, 27)] = "Christmas Day (additional)"
        out[date(year, 12, 28)] = "Boxing Day (additional)"
    elif xmas.weekday() == 6:  # Sun: Boxing Day is Mon 26, Christmas additional Tue 27
        out[date(year, 12, 27)] = "Christmas Day (additional)"
    elif boxing.weekday() == 5:  # Christmas Fri, Boxing Sat: additional Mon 28
        out[date(year, 12, 28)] = "Boxing Day (additional)"
    return out


def _configured() -> dict[date, str]:
    s = get_settings()
    out = {}
    for raw, name in ((s.grand_final_fridays, "Friday before the AFL Grand Final"),
                      (s.extra_public_holidays, "Public holiday")):
        for part in raw.split(","):
            if part.strip():
                out[date.fromisoformat(part.strip())] = name
    return out


def holiday(d: date) -> str | None:
    """The holiday's name if `d` is a Victorian public holiday, else None."""
    return _fixed(d.year).get(d) or _configured().get(d)
