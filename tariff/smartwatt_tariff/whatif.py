"""Naive versus true saving.

US26 requires BOTH shown. The gap between them is the product's entire
argument: under a flat-band tariff, a saving that drops the household back
under a boundary is worth many times what a nominal-rate calculation says.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .bill import evaluate, rate_for
from .display import SEN_PER_RINGGIT, to_2dp
from .loader import TariffConfig


@dataclass(frozen=True, slots=True)
class WhatIf:
    kwh_saved: Decimal
    #: What a nominal-rate calculation would claim.
    naive_saving: Decimal
    #: Two full bill evaluations, differenced.
    true_saving: Decimal
    #: Whether the saving drops the household back under a band boundary.
    crosses_back: bool
    #: true_saving / naive_saving, or 1 when there is nothing to divide
    #: because naive_saving is zero. Above 1 when the saving drops the
    #: household back under a band boundary; 0 when the RM 5.00 minimum
    #: charge floors both bills and the saving buys nothing at all.
    multiple: Decimal

    def display(self) -> dict[str, str]:
        """Round to 2dp for presentation. Money crosses the wire as strings."""
        return {
            "kwh_saved": to_2dp(self.kwh_saved),
            "naive_saving": to_2dp(self.naive_saving),
            "true_saving": to_2dp(self.true_saving),
            "multiple": to_2dp(self.multiple),
            "crosses_back": str(self.crosses_back),
        }


def whatif(
    projected_kwh: Decimal,
    kwh_saved: Decimal,
    on: date,
    *,
    config: TariffConfig | None = None,
) -> WhatIf:
    """Compare a nominal-rate saving against the real one."""
    saved = min(kwh_saved, projected_kwh)
    reduced = projected_kwh - saved

    rate_rm = rate_for(projected_kwh, config) / SEN_PER_RINGGIT
    naive = saved * rate_rm

    true = (
        evaluate(projected_kwh, on, config=config).total
        - evaluate(reduced, on, config=config).total
    )

    crosses_back = rate_for(reduced, config) != rate_for(projected_kwh, config)
    multiple = (true / naive) if naive > 0 else Decimal(1)

    return WhatIf(
        kwh_saved=saved,
        naive_saving=naive,
        true_saving=true,
        crosses_back=crosses_back,
        multiple=multiple,
    )
