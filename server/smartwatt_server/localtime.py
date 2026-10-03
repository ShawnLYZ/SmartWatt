"""The only module in the server permitted to touch timezones.

The IMPLEMENTATION lives in smartwatt_tariff.kuching, because S2 needs
Asia/Kuching for billing months and has no dependency on this package.
This module re-exports it and adds display formatting, so there is still
exactly one implementation.
"""

from __future__ import annotations

from datetime import timedelta

from smartwatt_tariff.kuching import (
    KUCHING,
    billing_month,
    days_elapsed_in_month,
    days_in_month,
    month_bounds,
    to_kuching,
)

__all__ = [
    "KUCHING", "billing_month", "day_bounds", "days_elapsed_in_month",
    "days_in_month", "month_bounds", "to_kuching", "week_bounds", "fmt",
]

# Asia/Kuching is a fixed +08:00 with no DST, so adding a timedelta to a
# local datetime cannot land on a skipped or repeated wall-clock hour and
# needs no normalisation. Anywhere else these two would need writing
# through UTC.


def day_bounds(ts: float) -> tuple[float, float]:
    """UTC epoch seconds of the local day's first and last instants.

    Same shape as ``month_bounds``: ``(start, end)`` where ``start`` is
    local midnight and ``end`` is local midnight the following day. What
    makes "today" on the dashboard mean the day the householder is having
    -- a rolling 24 hours at 09:00 would be two thirds yesterday.
    """
    start = to_kuching(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def week_bounds(ts: float) -> tuple[float, float]:
    """UTC epoch seconds of the local week's first and last instants.

    The week starts at local midnight on MONDAY: ``weekday()`` counts from
    Monday, and a week that starts on a weekday boundary drifting with the
    clock is not a week anyone recognises.
    """
    start = to_kuching(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=start.weekday())
    return start.timestamp(), (start + timedelta(days=7)).timestamp()


def fmt(ts: float) -> str:
    """Human-readable Asia/Kuching timestamp for display and logs."""
    return to_kuching(ts).strftime("%Y-%m-%d %H:%M:%S %Z")
