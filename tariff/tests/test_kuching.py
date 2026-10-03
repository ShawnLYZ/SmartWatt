from datetime import datetime, timezone
from decimal import Decimal

import pytest

from smartwatt_tariff.kuching import (
    KUCHING,
    billing_month,
    days_elapsed_in_month,
    days_in_month,
    month_bounds,
    to_kuching,
)


def _utc(year, month, day, hour=0, minute=0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_kuching_is_utc_plus_eight():
    moment = to_kuching(_utc(2026, 8, 13, 0, 0))
    assert moment.hour == 8
    assert moment.utcoffset().total_seconds() == 8 * 3600


def test_month_rollover_is_local_not_utc():
    """US28. Computing rollover in UTC would shift every cliff calculation
    by eight hours."""
    # 23:00 UTC on 31 August is 07:00 on 1 September in Kuching.
    assert billing_month(_utc(2026, 8, 31, 23, 0)) == (2026, 9)
    # 15:00 UTC on 31 August is still 23:00 on 31 August locally.
    assert billing_month(_utc(2026, 8, 31, 15, 0)) == (2026, 8)


def test_month_bounds_are_local_midnights():
    start, end = month_bounds(_utc(2026, 8, 13, 12, 0))
    assert to_kuching(start).day == 1
    assert to_kuching(start).hour == 0
    assert to_kuching(start).month == 8
    assert to_kuching(end).month == 9
    assert to_kuching(end).day == 1


def test_month_bounds_span_31_days_in_august():
    start, end = month_bounds(_utc(2026, 8, 13, 12, 0))
    assert (end - start) / 86400 == pytest.approx(31.0)


@pytest.mark.parametrize(
    "month,expected",
    [(1, 31), (2, 28), (4, 30), (8, 31), (12, 31)],
)
def test_days_in_month(month, expected):
    assert days_in_month(_utc(2026, month, 15, 12, 0)) == Decimal(expected)


def test_february_leap_year():
    assert days_in_month(_utc(2028, 2, 15, 12, 0)) == Decimal(29)


def test_days_elapsed_is_fractional():
    """Integer days would make the projection jump once a day."""
    # Local noon on the 1st is half a day into the month.
    elapsed = days_elapsed_in_month(_utc(2026, 8, 1, 4, 0))
    assert elapsed == pytest.approx(Decimal("0.5"), abs=Decimal("0.001"))


def test_days_elapsed_at_local_month_start_is_zero():
    start, _ = month_bounds(_utc(2026, 8, 13, 12, 0))
    assert days_elapsed_in_month(start) == Decimal(0)


def test_days_elapsed_never_exceeds_days_in_month():
    _, end = month_bounds(_utc(2026, 8, 13, 12, 0))
    assert days_elapsed_in_month(end - 1) < days_in_month(end - 1)


def test_returns_decimal_not_float():
    assert isinstance(days_in_month(_utc(2026, 8, 13)), Decimal)
    assert isinstance(days_elapsed_in_month(_utc(2026, 8, 13)), Decimal)


def test_december_rolls_into_next_year():
    assert billing_month(_utc(2026, 12, 31, 20, 0)) == (2027, 1)
