"""FX market hours: Monday 05:00 Sydney to Friday 17:00 New York, across DST."""

from datetime import datetime, timedelta, timezone

import pytest

from execution_cost_tracker import market_hours as mh


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def syd(s):
    return datetime.fromisoformat(s).replace(tzinfo=mh.SYDNEY)


def ny(s):
    return datetime.fromisoformat(s).replace(tzinfo=mh.NEW_YORK)


@pytest.mark.parametrize(
    "opening",
    [
        "2026-10-05T05:00",  # Sydney AEDT (+11), New York EDT (-4): Sun 14:00 NY
        "2027-01-04T05:00",  # AEDT (+11), EST (-5): Sun 13:00 NY
        "2027-07-05T05:00",  # AEST (+10), EDT (-4): Sun 15:00 NY
        "2027-03-29T05:00",  # NY already on EDT, Sydney still AEDT until April
    ],
)
def test_opens_monday_5am_sydney(opening):
    t = syd(opening)
    assert t.weekday() == 0
    assert not mh.is_open(t - timedelta(minutes=1))
    assert mh.is_open(t)
    assert mh.next_change(t - timedelta(minutes=1)) == t.astimezone(timezone.utc)


@pytest.mark.parametrize("close", ["2026-10-09T17:00", "2027-01-08T17:00", "2027-07-09T17:00"])
def test_closes_friday_5pm_new_york(close):
    t = ny(close)
    assert t.weekday() == 4
    assert mh.is_open(t - timedelta(minutes=1))
    assert not mh.is_open(t)
    assert mh.next_change(t - timedelta(minutes=1)) == t.astimezone(timezone.utc)


def test_midweek_open_weekend_closed():
    assert mh.is_open(ny("2026-10-07T03:00"))      # Wednesday
    assert not mh.is_open(ny("2026-10-10T12:00"))  # Saturday
    assert not mh.is_open(ny("2026-10-11T09:00"))  # Sunday morning NY = Sunday night Sydney
    assert mh.is_open(ny("2026-10-11T20:00"))      # Sunday evening NY = Monday morning Sydney


def test_this_weekend_example():
    # Sunday 4 Oct 2026, 15:25 New York: Sydney is Monday 06:25, so the market is open.
    assert mh.is_open(ny("2026-10-04T15:25"))
    assert not mh.is_open(ny("2026-10-04T13:59"))


def test_next_change_when_closed_on_saturday():
    t = ny("2026-10-10T12:00")
    assert mh.next_change(t) == syd("2026-10-12T05:00").astimezone(timezone.utc)


def test_status_and_naive_datetime():
    s = mh.status(utc("2026-10-07T12:00"))
    assert s["open"] is True and s["next_change"].startswith("2026-10-09T21:00")
    with pytest.raises(ValueError):
        mh.is_open(datetime(2026, 10, 7, 12, 0))
