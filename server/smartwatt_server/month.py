"""Assemble /api/month and /api/whatif.

Money crosses the wire as STRINGS, through smartwatt_tariff.display.to_2dp -
the one place S2 turns a Decimal into a string for presentation. Building a
second rounding helper here would be exactly the drift that module warns
against, so this file has none. kWh, kg and dimensionless-ratio quantities
get their own 3-place helper below, because S2 ships no equivalent for
those and they are not money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from smartwatt_tariff import carbon as carbon_engine
from smartwatt_tariff.bill import evaluate, evaluate_undiscounted
from smartwatt_tariff.cliff import band_step, marginal, next_boundary, project
from smartwatt_tariff.display import to_2dp
from smartwatt_tariff.whatif import whatif

from .ledger import RESIDUAL_ID
from .localtime import month_bounds, to_kuching
from .store import Store

_THREE = Decimal("0.001")


def _to_3dp(value: Decimal) -> str:
    """Round a kWh, kg or dimensionless-ratio quantity to 3 places.

    Not money - to_2dp is for money and only money. This is for the other
    kind of number that shows up on this dashboard: energy, carbon mass,
    and ratios like the whatif multiple.
    """
    return str(value.quantize(_THREE, rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class _ApplianceMonth:
    """One appliance's share of the current billing month so far."""

    kwh: Decimal
    w_mean: Decimal


def _month_ledger(store: Store, now_ts: float) -> dict[str, _ApplianceMonth]:
    """kWh and mean watts per appliance for the current local billing month.

    w_mean travels alongside kwh because whatif_payload needs an
    appliance's measured mean power, not just its total energy, to turn a
    requested number of hours into a saving.
    """
    start, end = month_bounds(now_ts)
    return {
        row["appliance_id"]: _ApplianceMonth(
            kwh=Decimal(str(row["wh"] or 0.0)) / Decimal(1000),
            w_mean=Decimal(str(row["w_mean"] or 0.0)),
        )
        for row in store.ledger(start, min(end, now_ts))
    }


def _culprit(
    per_appliance: dict[str, _ApplianceMonth],
    projected: Decimal,
    boundary_kwh: Decimal,
    scale: Decimal,
) -> dict | None:
    """The appliance responsible for pushing the projection over. US24.

    Defined as the largest projected contributor whose own projected
    consumption exceeds the overshoot - that is, one whose removal alone
    would get the household back under. If no single appliance suffices,
    the largest contributor is named with ``sufficient_alone: False``,
    rather than naming something whose removal would not actually help.

    Pseudo-appliances are excluded: __residual__ is not actionable.
    """
    overshoot = projected - boundary_kwh
    if overshoot <= 0:
        return None

    actionable = {
        appliance_id: entry.kwh * scale
        for appliance_id, entry in per_appliance.items()
        if appliance_id != RESIDUAL_ID
    }
    if not actionable:
        return None

    sufficient = {
        appliance_id: kwh
        for appliance_id, kwh in actionable.items()
        if kwh >= overshoot
    }
    pool = sufficient or actionable
    appliance_id = max(pool, key=lambda key: pool[key])

    return {
        "appliance_id": appliance_id,
        "projected_kwh": _to_3dp(pool[appliance_id]),
        "sufficient_alone": bool(sufficient),
    }


def month_payload(store: Store, now_ts: float) -> dict:
    per_appliance = _month_ledger(store, now_ts)
    mtd = sum((entry.kwh for entry in per_appliance.values()), Decimal(0))
    projected = project(mtd, now_ts)
    on = to_kuching(now_ts).date()

    bill = evaluate(mtd, on)
    projected_bill = evaluate(projected, on)
    undiscounted = evaluate_undiscounted(mtd, on)

    # Anchored on MTD, not the projection. next_boundary(projected) looks
    # for the band edge ABOVE its argument, so next_boundary(projected)
    # is almost always None (the projection has usually already run past
    # the household's current band) - which silently disables the culprit
    # feature. The panel describes the band the household is IN right now.
    boundary = next_boundary(mtd)
    if boundary is not None:
        scale = (projected / mtd) if mtd > 0 else Decimal(1)
        cliff = {
            "boundary_kwh": str(boundary.up_to_kwh),
            "kwh_remaining": _to_3dp(boundary.kwh_remaining),
            "current_rate_sen": str(boundary.current_rate_sen),
            "next_rate_sen": str(boundary.next_rate_sen),
            "band_step": to_2dp(band_step(boundary.up_to_kwh, on)),
            "marginal": to_2dp(marginal(mtd, on)),
            "culprit": _culprit(per_appliance, projected, boundary.up_to_kwh, scale),
        }
    else:
        cliff = {
            "boundary_kwh": None, "kwh_remaining": None,
            "current_rate_sen": None, "next_rate_sen": None,
            "band_step": "0.00", "marginal": to_2dp(marginal(mtd, on)),
            "culprit": None,
        }

    local = carbon_engine.local(mtd)
    hypothetical = carbon_engine.hypothetical(mtd, "peninsular")
    comparison = carbon_engine.comparison()
    tree = carbon_engine.tree_years(local.kg)

    def framing(entry) -> dict:
        return {
            "region": entry.region,
            "kg": _to_3dp(entry.kg),
            "hypothetical": entry.hypothetical,
            "provisional": entry.factor.provisional,
            "factor_kg_per_kwh": str(entry.factor.kg_per_kwh),
            "provenance": {
                "source": entry.factor.provenance.source,
                "url": entry.factor.provenance.url,
                "vintage": entry.factor.provenance.vintage,
            },
        }

    return {
        "now_ts": now_ts,
        "mtd_kwh": float(mtd),
        "projected_kwh": float(projected),
        "bill": bill.display(),
        "bill_projected": projected_bill.display(),
        "bill_undiscounted": undiscounted.display(),
        "discount_active": bill.discounted,
        "cliff": cliff,
        "carbon": {
            "local": framing(local),
            "hypothetical": framing(hypothetical),
            "comparison": {
                "ratio": _to_3dp(comparison.ratio),
                "trend": {str(y): str(v) for y, v in comparison.trend.items()},
                "statement": comparison.statement,
                "displaced_framing": comparison.displaced_framing,
            },
            "tree_years": None if tree is None else _to_3dp(tree),
        },
    }


def whatif_payload(
    store: Store, appliance_id: str, hours: float, now_ts: float
) -> dict:
    """US25, US26. 'What if I stopped doing this?'

    The saving is grounded in the appliance's measured mean power times the
    requested hours, capped at what it is actually projected to consume
    this month. (An earlier version derived an average watt figure from
    the requested hours and then multiplied it back by those same hours,
    which cancels out to the appliance's whole-month projection regardless
    of what was asked - degenerate, not merely imprecise.)
    """
    per_appliance = _month_ledger(store, now_ts)
    if appliance_id not in per_appliance:
        raise KeyError(f"no ledger entry for {appliance_id!r} this month")

    mtd = sum((entry.kwh for entry in per_appliance.values()), Decimal(0))
    projected = project(mtd, now_ts)
    scale = (projected / mtd) if mtd > 0 else Decimal(1)

    entry = per_appliance[appliance_id]
    appliance_projected_kwh = entry.kwh * scale
    saved = min(
        appliance_projected_kwh,
        Decimal(str(hours)) * entry.w_mean / Decimal(1000),
    )

    result = whatif(projected, saved, to_kuching(now_ts).date())
    return {
        "appliance_id": appliance_id,
        "hours": hours,
        "kwh_saved": _to_3dp(result.kwh_saved),
        "naive_saving": to_2dp(result.naive_saving),
        "true_saving": to_2dp(result.true_saving),
        "crosses_back": result.crosses_back,
        "multiple": _to_3dp(result.multiple),
    }
