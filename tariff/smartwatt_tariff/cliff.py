"""Band-crossing analysis.

Marginal cost is ALWAYS the difference of two full bill evaluations, never
the nominal rate multiplied by a delta. Under a flat-band tariff those are
wildly different numbers at a boundary, and the difference is the entire
product.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .bill import evaluate
from .kuching import days_elapsed_in_month, days_in_month
from .loader import TariffConfig, resolve_tariff


@dataclass(frozen=True, slots=True)
class Boundary:
    up_to_kwh: Decimal
    kwh_remaining: Decimal
    current_rate_sen: Decimal
    next_rate_sen: Decimal


def marginal(
    kwh: Decimal,
    on: date,
    *,
    delta: Decimal = Decimal(1),
    config: TariffConfig | None = None,
) -> Decimal:
    """Cost of the next ``delta`` kWh: two full bill evaluations, differenced."""
    cfg = resolve_tariff(config)
    return (
        evaluate(kwh + delta, on, config=cfg).total
        - evaluate(kwh, on, config=cfg).total
    )


def next_boundary(
    kwh: Decimal, *, config: TariffConfig | None = None
) -> Boundary | None:
    """The next band edge above ``kwh``, or ``None`` in the open-ended band."""
    cfg = resolve_tariff(config)
    bounded = [b for b in cfg.bands if b.up_to_kwh is not None]

    for index, band in enumerate(bounded):
        if kwh <= band.up_to_kwh:
            next_rate = (
                bounded[index + 1].sen_per_kwh
                if index + 1 < len(bounded)
                else cfg.bands[-1].sen_per_kwh
            )
            return Boundary(
                up_to_kwh=band.up_to_kwh,
                kwh_remaining=band.up_to_kwh - kwh,
                current_rate_sen=band.sen_per_kwh,
                next_rate_sen=next_rate,
            )
    return None


def band_step(
    boundary_kwh: Decimal,
    on: date,
    *,
    config: TariffConfig | None = None,
) -> Decimal:
    """The re-pricing discontinuity alone, with no marginal unit included.

    This is the number that makes US22 legible: crossing the boundary
    re-prices the entire month, not just the next unit.

    Expects ``boundary_kwh`` to be a real band edge, as returned by
    ``next_boundary(...).up_to_kwh``. Passed an interior value instead, it
    still returns a well-defined number - the cost of re-pricing that
    quantity from its current band's rate to the next band's rate - but
    that is a different and less useful figure than the discontinuity at
    the boundary itself.
    """
    cfg = resolve_tariff(config)
    boundary = next_boundary(boundary_kwh, config=cfg)
    if boundary is None:
        return Decimal(0)

    at_current = evaluate(
        boundary_kwh, on, config=cfg, rate_override=boundary.current_rate_sen
    ).total
    at_next = evaluate(
        boundary_kwh, on, config=cfg, rate_override=boundary.next_rate_sen
    ).total
    return at_next - at_current


def project(mtd_kwh: Decimal, now_ts: float) -> Decimal:
    """Linear run-rate projection to the end of the local billing month.

    Deliberately simple, and labelled a projection wherever it is shown.
    Anything cleverer would be a model presented as a measurement.
    """
    elapsed = days_elapsed_in_month(now_ts)
    if elapsed <= 0:
        return mtd_kwh
    return mtd_kwh * days_in_month(now_ts) / elapsed
