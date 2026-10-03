"""Grid carbon in three framings.

1. Local impact at Sarawak's factor - the true number for this household.
2. Transferred impact at another region's factor - ALWAYS labelled
   hypothetical, never summed with the local figure.
3. Displaced generation - presented qualitatively as a config string. There
   is deliberately no numeric field for it, because inventing a marginal
   intensity would misrepresent a guess as a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .display import to_2dp
from .loader import CarbonConfig, Factor, resolve_carbon

_LOCAL_REGION = "sarawak"


@dataclass(frozen=True, slots=True)
class Carbon:
    kg: Decimal
    region: str
    factor: Factor
    #: True for any region other than the household's own. Never summed
    #: with a local figure.
    hypothetical: bool

    def display(self) -> dict[str, str]:
        """Round to 2dp for presentation. Money crosses the wire as strings."""
        return {
            "kg": to_2dp(self.kg),
            "region": self.region,
            "hypothetical": str(self.hypothetical),
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    ratio: Decimal
    trend: dict[int, Decimal]
    statement: str
    #: Qualitative by design. There is no numeric counterpart to this field.
    displaced_framing: str


def factors(*, config: CarbonConfig | None = None) -> list[Factor]:
    return list(resolve_carbon(config).factors)


def _factor(region: str, config: CarbonConfig) -> Factor:
    for factor in config.factors:
        if factor.region == region:
            return factor
    raise KeyError(f"no emission factor configured for region {region!r}")


def local(kwh: Decimal, *, config: CarbonConfig | None = None) -> Carbon:
    """This household's real impact, at Sarawak's grid factor."""
    cfg = resolve_carbon(config)
    factor = _factor(_LOCAL_REGION, cfg)
    return Carbon(
        kg=kwh * factor.kg_per_kwh,
        region=_LOCAL_REGION,
        factor=factor,
        hypothetical=False,
    )


def hypothetical(
    kwh: Decimal, region: str, *, config: CarbonConfig | None = None
) -> Carbon:
    """The same consumption at another region's factor. US31.

    Carries ``hypothetical=True`` whenever ``region`` differs from the
    household's own. The transferability argument is made explicitly
    rather than smuggled in.
    """
    cfg = resolve_carbon(config)
    factor = _factor(region, cfg)
    return Carbon(
        kg=kwh * factor.kg_per_kwh,
        region=region,
        factor=factor,
        hypothetical=region != _LOCAL_REGION,
    )


def comparison(*, config: CarbonConfig | None = None) -> Comparison:
    cfg = resolve_carbon(config)
    return Comparison(
        ratio=(
            _factor("peninsular", cfg).kg_per_kwh
            / _factor(_LOCAL_REGION, cfg).kg_per_kwh
        ),
        trend=dict(cfg.peninsular_trend),
        statement=cfg.cleaner_grid_statement,
        displaced_framing=cfg.displaced_framing,
    )


def tree_years(
    kg: Decimal, *, config: CarbonConfig | None = None
) -> Decimal | None:
    """Mature-tree-years equivalent. US34.

    Returns ``None`` when no sourced factor is configured. No figure is
    invented to fill the gap: the dashboard omits the line entirely, which
    is the honest outcome, and the server still boots because an optional
    equivalence is not worth blocking startup over.
    """
    cfg = resolve_carbon(config)
    if cfg.tree_year_kg is None:
        return None
    return kg / cfg.tree_year_kg
