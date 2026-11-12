"""
Unit tests for the forecast helpers shared by Pricing modules.
"""

from datetime import datetime, timedelta, timezone

import pytest

from TWCManager.Pricing.Forecast import (
    cheapestWindow,
    latchedWindow,
    priceRange,
    serialise,
    windowActive,
)

START = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)


def _fc(prices, start=START):
    return [
        {"timestamp": start + timedelta(minutes=30 * i), "importPrice": p}
        for i, p in enumerate(prices)
    ]


def test_cheapest_window_picks_lowest_average():
    best = cheapestWindow(_fc([4, 4, 1, 1, 3, 3]), 1)
    assert best["avgPrice"] == 1
    assert best["startTimestamp"] == "2026-07-15T01:00:00+00:00"
    assert best["totalCost"] == 1


def test_tie_keeps_earliest():
    best = cheapestWindow(_fc([2, 2, 2, 2]), 1)
    assert best["startTimestamp"] == "2026-07-15T00:00:00+00:00"


def test_window_never_spans_a_gap():
    fc = _fc([5, 1, 9, 1, 5])
    del fc[2]  # 01:00 missing: 00:30 and 01:30 are not adjacent
    best = cheapestWindow(fc, 1)
    assert best["avgPrice"] == 3  # a window spanning the gap would average 1
    assert best["startTimestamp"] == "2026-07-15T00:00:00+00:00"


def test_empty_and_out_of_range():
    assert cheapestWindow([], 1) is None
    assert cheapestWindow(_fc([1, 1]), 0) is None
    assert cheapestWindow(_fc([1, 1]), 25) is None
    assert cheapestWindow(_fc([1, 1]), 2) is None  # longer than the forecast
    assert priceRange([]) is None


def test_price_range():
    assert priceRange(_fc([3, 1, 2])) == (1, 3)


def test_hour_filter_wraps_midnight():
    # Cheapest 1h is at 12:00, but only starts from 22:00 to 06:00 qualify
    prices = [5] * 48
    prices[24:26] = [0, 0]  # 12:00
    prices[46:48] = [2, 2]  # 23:00
    best = cheapestWindow(_fc(prices), 1, 22, 6, tz=timezone.utc)
    assert best["startTimestamp"] == "2026-07-15T23:00:00+00:00"
    assert best["startHour"] == 23


def test_window_finishes_by_end_hour():
    # 06:00-15:00 is cheapest, but a 22:00-07:00 window must end by 07:00, so
    # the cheapest 5 hours that fit start at 02:00
    prices = [5] * 96
    prices[12:30] = [1] * 18  # 06:00-15:00
    prices[52:62] = [3] * 10  # 02:00-07:00 the next day
    best = cheapestWindow(_fc(prices), 5, 22, 7, tz=timezone.utc)
    assert best["startTimestamp"] == "2026-07-16T02:00:00+00:00"
    assert cheapestWindow(_fc(prices), 10, 22, 7, tz=timezone.utc) is None


def test_window_active():
    start = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
    assert windowActive(start.isoformat(), 2, start) == 1
    assert windowActive(start.isoformat(), 2, start + timedelta(minutes=119)) == 1
    assert windowActive(start.isoformat(), 2, start + timedelta(hours=2)) == 0
    assert windowActive(start.isoformat(), 2, start - timedelta(minutes=1)) == 0
    assert windowActive(None, 2, start) == 0


def test_serialise():
    out = serialise(_fc([1] * 10), 2)
    assert len(out) == 4
    assert out[0]["timestamp"] == "2026-07-15T00:00:00+00:00"
    assert out[0]["importPrice"] == 1


def test_window_spans_dst_change():
    zoneinfo = pytest.importorskip("zoneinfo")
    ny = zoneinfo.ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)  # 00:00 EDT
    fc = [
        {
            "timestamp": (start + timedelta(minutes=30 * i)).astimezone(ny),
            "importPrice": 1,
        }
        for i in range(12)
    ]
    best = cheapestWindow(fc, 6, tz=ny)
    assert best["startTimestamp"] == "2026-11-01T00:00:00-04:00"


def test_latched_window_holds_until_it_ends():
    prev = "2026-07-15T01:00:00+00:00"
    inside = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)
    assert latchedWindow(prev, 4, inside, lambda: {"startTimestamp": "x"}) == prev
    after = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)
    assert latchedWindow(prev, 4, after, lambda: {"startTimestamp": "x"}) == "x"
    assert latchedWindow(None, 4, after, lambda: None) is None
