"""Parse configuration into typed Decimal structures.

Money arrives as TOML strings and becomes Decimal here. Nothing downstream
should ever see a float where money is concerned.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / "config"


@dataclass
class Provenance:
    source: str
    url: str
    vintage: str


@dataclass(frozen=True, slots=True)
class Band:
    #: Inclusive upper bound of the band. ``None`` for the open-ended band.
    up_to_kwh: Decimal | None
    sen_per_kwh: Decimal


@dataclass(frozen=True, slots=True)
class Charges:
    minimum_monthly_rm: Decimal
    service_tax_rate: Decimal
    service_tax_exempt_kwh: Decimal
    minimum_billing_days: int


@dataclass
class Discount:
    rate: Decimal
    base: str
    effective_from: date
    effective_to: date


@dataclass
class TariffConfig:
    bands: list[Band]
    charges: Charges
    discount: Discount
    provenance: Provenance


@dataclass
class Factor:
    region: str
    kg_per_kwh: Decimal
    provenance: Provenance
    provisional: bool


@dataclass
class CarbonConfig:
    factors: list[Factor]
    peninsular_trend: dict[int, Decimal]
    displaced_framing: str
    cleaner_grid_statement: str
    tree_year_kg: Decimal | None
    tree_year_provenance: Provenance | None


def load_tariff(path: Path | None = None) -> TariffConfig:
    raw = tomllib.loads(
        (path or CONFIG_DIR / "tariff.toml").read_text(encoding="utf-8")
    )
    bands = [
        Band(
            up_to_kwh=(
                Decimal(item["up_to_kwh"]) if "up_to_kwh" in item else None
            ),
            sen_per_kwh=Decimal(item["sen_per_kwh"]),
        )
        for item in raw["bands"]
    ]
    charges_raw = raw["charges"]
    discount_raw = raw["discount"]
    return TariffConfig(
        bands=bands,
        charges=Charges(
            minimum_monthly_rm=Decimal(charges_raw["minimum_monthly_rm"]),
            service_tax_rate=Decimal(charges_raw["service_tax_rate"]),
            service_tax_exempt_kwh=Decimal(charges_raw["service_tax_exempt_kwh"]),
            minimum_billing_days=int(charges_raw["minimum_billing_days"]),
        ),
        discount=Discount(
            rate=Decimal(discount_raw["rate"]),
            base=discount_raw["base"],
            effective_from=discount_raw["effective_from"],
            effective_to=discount_raw["effective_to"],
        ),
        provenance=Provenance(**raw["provenance"]),
    )


def load_carbon(path: Path | None = None) -> CarbonConfig:
    raw = tomllib.loads(
        (path or CONFIG_DIR / "carbon.toml").read_text(encoding="utf-8")
    )
    factors = [
        Factor(
            region=item["region"],
            kg_per_kwh=Decimal(item["kg_per_kwh"]),
            provisional=bool(item["provisional"]),
            provenance=Provenance(
                source=item["source"], url=item["url"], vintage=item["vintage"]
            ),
        )
        for item in raw["factors"]
    ]
    equivalence = raw.get("equivalence", {})
    tree_kg = (
        Decimal(equivalence["tree_year_kg"])
        if "tree_year_kg" in equivalence
        else None
    )
    tree_provenance = (
        Provenance(
            source=equivalence["source"],
            url=equivalence["url"],
            vintage=equivalence["vintage"],
        )
        if tree_kg is not None
        else None
    )
    return CarbonConfig(
        factors=factors,
        peninsular_trend={
            int(year): Decimal(value)
            for year, value in raw["peninsular_trend"].items()
        },
        displaced_framing=raw["framings"]["displaced"].strip(),
        cleaner_grid_statement=raw["framings"]["cleaner_grid"].strip(),
        tree_year_kg=tree_kg,
        tree_year_provenance=tree_provenance,
    )


@lru_cache(maxsize=1)
def _shipped_tariff() -> TariffConfig:
    return load_tariff()


def resolve_tariff(config: TariffConfig | None) -> TariffConfig:
    """The caller's configuration, or the shipped one, parsed once per process."""
    return config if config is not None else _shipped_tariff()


@lru_cache(maxsize=1)
def _shipped_carbon() -> CarbonConfig:
    return load_carbon()


def resolve_carbon(config: CarbonConfig | None) -> CarbonConfig:
    """The caller's configuration, or the shipped one, parsed once per process."""
    return config if config is not None else _shipped_carbon()
