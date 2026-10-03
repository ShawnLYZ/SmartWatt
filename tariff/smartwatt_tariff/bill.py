"""Tariff D bill evaluation.

    rate        = the single band rate selected by total monthly consumption
    energy      = max(minimum_charge, kwh * rate)
    service_tax = max(0, kwh - 600) * rate * 0.08
    discount    = discount_rate * (energy + service_tax)
    total       = energy + service_tax - discount

Flat-band, not progressive-block: the selected rate applies to EVERY unit
used that month, which is why crossing a boundary re-prices the whole month.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from .display import SEN_PER_RINGGIT, to_2dp
from .loader import TariffConfig, resolve_tariff


class BillingPeriodError(Exception):
    """Billing period is shorter than the configured minimum."""


@dataclass(frozen=True, slots=True)
class Bill:
    kwh: Decimal
    rate_sen: Decimal
    energy: Decimal
    service_tax: Decimal
    discount: Decimal
    total: Decimal
    discounted: bool

    def display(self) -> dict[str, str]:
        """Round to sen for presentation. Money crosses the wire as strings."""
        return {
            "kwh": str(self.kwh),
            "rate_sen": str(self.rate_sen),
            "energy": to_2dp(self.energy),
            "service_tax": to_2dp(self.service_tax),
            "discount": to_2dp(self.discount),
            "total": to_2dp(self.total),
        }


def rate_for(kwh: Decimal, config: TariffConfig | None = None) -> Decimal:
    """The single band rate, in sen/kWh, selected by total consumption."""
    cfg = resolve_tariff(config)
    for band in cfg.bands:
        if band.up_to_kwh is not None and kwh <= band.up_to_kwh:
            return band.sen_per_kwh
    return cfg.bands[-1].sen_per_kwh


def evaluate(
    kwh: Decimal,
    on: date,
    *,
    config: TariffConfig | None = None,
    rate_override: Decimal | None = None,
    billing_days: int = 30,
) -> Bill:
    """Evaluate a full month's bill.

    Args:
        rate_override: Price this consumption at a specific rate instead of
            the one its own total would select. Used by ``cliff.band_step``
            to isolate the re-pricing discontinuity from the marginal unit.
            Must be positive.
        billing_days: Length of the billing period. Guarded against the
            configured minimum; a calendar month always satisfies it.
    """
    cfg = resolve_tariff(config)

    if billing_days < cfg.charges.minimum_billing_days:
        raise BillingPeriodError(
            f"billing period of {billing_days} days is shorter than the "
            f"configured minimum of {cfg.charges.minimum_billing_days}; "
            "service-tax semantics below this are unresolved"
        )

    if rate_override is not None and rate_override <= 0:
        raise ValueError(
            f"rate_override must be positive, got {rate_override!r}"
        )

    rate_sen = rate_override if rate_override is not None else rate_for(kwh, cfg)
    rate_rm = rate_sen / SEN_PER_RINGGIT

    energy = max(cfg.charges.minimum_monthly_rm, kwh * rate_rm)

    taxable = max(Decimal(0), kwh - cfg.charges.service_tax_exempt_kwh)
    service_tax = taxable * rate_rm * cfg.charges.service_tax_rate

    in_window = cfg.discount.effective_from <= on <= cfg.discount.effective_to
    if in_window:
        # Explicit three-way: an unrecognised base must raise rather than
        # silently falling into the energy-only branch. Base is TOTAL by
        # config, not energy alone - that was the historical bug.
        if cfg.discount.base == "total":
            base = energy + service_tax
        elif cfg.discount.base == "energy":
            base = energy
        else:
            raise ValueError(
                f"unknown discount base: {cfg.discount.base!r}"
            )
        discount = cfg.discount.rate * base
    else:
        discount = Decimal(0)

    return Bill(
        kwh=kwh,
        rate_sen=rate_sen,
        energy=energy,
        service_tax=service_tax,
        discount=discount,
        total=energy + service_tax - discount,
        discounted=in_window,
    )


def evaluate_undiscounted(
    kwh: Decimal, on: date, *, config: TariffConfig | None = None
) -> Bill:
    """What the household will pay once the state discount ends. US27."""
    bill = evaluate(kwh, on, config=config)
    return replace(
        bill,
        discount=Decimal(0),
        total=bill.energy + bill.service_tax,
        discounted=False,
    )
