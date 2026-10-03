"""The one place a Decimal becomes a string for presentation.

Everything upstream stays at full precision. Rounding happens here and
nowhere else, so a consumer never has to invent its own and the two cannot
drift apart.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: Sen per ringgit. Rates are published in sen; money is carried in ringgit.
SEN_PER_RINGGIT = Decimal(100)

_TWO_PLACES = Decimal("0.01")


def to_2dp(value: Decimal) -> str:
    """Round to two decimal places for display. Money crosses the wire as
    a string so a browser cannot reintroduce binary floating point."""
    return str(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))
