"""When the interbank FX market is open, so a market mid can be quoted.

The week runs from Monday 05:00 in Sydney to Friday 17:00 in New York. Both
cities observe daylight saving on different schedules, so the open falls on
Sunday afternoon New York time (13:00-15:00) depending on the season. All
conversions go through ``zoneinfo``, so DST is handled automatically.

Public holidays (e.g. Christmas, New Year) aren't modelled; on those days the
market mid simply goes stale and the page shows N/A for that reason instead.

Pure functions: pass ``now`` explicitly; nothing here reads the clock.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
NEW_YORK = ZoneInfo("America/New_York")
OPEN_LOCAL = time(5, 0)    # Monday, Sydney
CLOSE_LOCAL = time(17, 0)  # Friday, New York

MONDAY, FRIDAY, SATURDAY, SUNDAY = 0, 4, 5, 6


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (e.g. datetime.now(timezone.utc))")
    return now


def is_open(now: datetime) -> bool:
    """True between Monday 05:00 Sydney and Friday 17:00 New York."""
    now = _aware(now)
    ny = now.astimezone(NEW_YORK)
    if ny.weekday() == SATURDAY:
        return False
    if ny.weekday() == FRIDAY and ny.time() >= CLOSE_LOCAL:
        return False
    if ny.weekday() == SUNDAY:
        # Sunday in New York is already Monday in Sydney; open from 05:00 there.
        syd = now.astimezone(SYDNEY)
        return syd.weekday() == MONDAY and syd.time() >= OPEN_LOCAL
    return True


def next_change(now: datetime) -> datetime:
    """UTC time of the next open (if closed) or close (if open)."""
    now = _aware(now)
    if is_open(now):
        ny = now.astimezone(NEW_YORK)
        friday = ny.date() + timedelta(days=(FRIDAY - ny.weekday()) % 7)
        return datetime.combine(friday, CLOSE_LOCAL, tzinfo=NEW_YORK).astimezone(timezone.utc)
    # Closed: the next Monday 05:00 in Sydney.
    syd = now.astimezone(SYDNEY)
    days = (MONDAY - syd.weekday()) % 7
    candidate = datetime.combine(syd.date() + timedelta(days=days), OPEN_LOCAL, tzinfo=SYDNEY)
    if candidate <= syd:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def status(now: datetime) -> dict:
    """``{"open": bool, "next_change": iso, "hours": text}`` for the API."""
    return {
        "open": is_open(now),
        "next_change": next_change(now).isoformat(),
        "hours": "Monday 05:00 Sydney to Friday 17:00 New York",
    }
