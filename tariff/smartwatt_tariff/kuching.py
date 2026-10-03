"""The one Asia/Kuching helper.

A billing month follows the LOCAL calendar month. Computing rollover in UTC
would shift every cliff calculation by eight hours, which is the difference
between warning a household before they cross a band boundary and warning
them after.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

KUCHING = ZoneInfo("Asia/Kuching")

_SECONDS_PER_DAY = Decimal(86400)


def to_kuching(ts: float) -> datetime:
    """Convert UTC epoch seconds to an Asia/Kuching aware datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(KUCHING)


def billing_month(ts: float) -> tuple[int, int]:
    """The ``(year, month)`` this instant belongs to, in local time."""
    local = to_kuching(ts)
    return local.year, local.month


def month_bounds(ts: float) -> tuple[float, float]:
    """UTC epoch seconds of the local month's first and last instants.

    Returns ``(start, end)`` where ``start`` is local midnight on the 1st and
    ``end`` is local midnight on the 1st of the following month.
    """
    local = to_kuching(ts)
    start = local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if local.month == 12:
        end = start.replace(year=local.year + 1, month=1)
    else:
        end = start.replace(month=local.month + 1)
    return start.timestamp(), end.timestamp()


def days_in_month(ts: float) -> Decimal:
    year, month = billing_month(ts)
    return Decimal(calendar.monthrange(year, month)[1])


def days_elapsed_in_month(ts: float) -> Decimal:
    """Fractional days since local midnight on the 1st.

    Fractional deliberately: an integer count would make the month-end
    projection jump once a day rather than track consumption smoothly.
    """
    start, _ = month_bounds(ts)
    return Decimal(str(ts - start)) / _SECONDS_PER_DAY
