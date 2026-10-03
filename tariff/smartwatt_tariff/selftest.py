"""Configuration integrity checks.

Runs at server startup. Empty provenance means the server refuses to boot:
a figure whose source cannot be shown is a figure that cannot be defended,
and non-negotiable #2 rules out presenting one.
"""

from __future__ import annotations

from .loader import (
    CarbonConfig,
    Provenance,
    TariffConfig,
    load_carbon,
    load_tariff,
)


class ConfigError(Exception):
    """Configuration is unusable. Never caught in the product; it stops boot."""


def _check_provenance(provenance: Provenance, what: str) -> None:
    for field_name in ("source", "url", "vintage"):
        if not str(getattr(provenance, field_name)).strip():
            raise ConfigError(
                f"empty provenance: {what} is missing {field_name!r}"
            )


def _check_bands(config: TariffConfig) -> None:
    bounded = [b for b in config.bands if b.up_to_kwh is not None]
    open_ended = [b for b in config.bands if b.up_to_kwh is None]

    if len(open_ended) != 1 or config.bands[-1].up_to_kwh is not None:
        raise ConfigError("bands must end with exactly one open-ended band")

    bounds = [b.up_to_kwh for b in bounded]
    if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
        raise ConfigError("band upper bounds must be strictly ascending")

    expected = ["100", "150", "200", "300", "400", "500", "700", "800", "1300"]
    if [str(b) for b in bounds] != expected:
        raise ConfigError(
            "band schedule is not contiguous with the published Tariff D "
            f"boundaries; expected {expected}, got {[str(b) for b in bounds]}"
        )

    for band in config.bands:
        if band.sen_per_kwh <= 0:
            raise ConfigError("band rates must be positive")


def _check_discount(config: TariffConfig) -> None:
    if config.discount.base not in ("total", "energy"):
        raise ConfigError("discount base must be 'total' or 'energy'")
    if config.discount.effective_from > config.discount.effective_to:
        raise ConfigError("discount window is inverted")
    if not (0 <= config.discount.rate <= 1):
        raise ConfigError("discount rate must be between 0 and 1")


def run_selftest(
    tariff: TariffConfig | None = None,
    carbon: CarbonConfig | None = None,
) -> None:
    """Validate configuration. Raises ConfigError on any failure."""
    tariff = tariff if tariff is not None else load_tariff()
    carbon = carbon if carbon is not None else load_carbon()

    _check_provenance(tariff.provenance, "tariff schedule")
    _check_bands(tariff)
    _check_discount(tariff)

    if not carbon.factors:
        raise ConfigError("no emission factors configured")
    for factor in carbon.factors:
        _check_provenance(factor.provenance, f"emission factor {factor.region!r}")
        if factor.kg_per_kwh <= 0:
            raise ConfigError(f"emission factor {factor.region!r} must be positive")

    if carbon.tree_year_kg is not None:
        if carbon.tree_year_provenance is None:
            raise ConfigError("tree-year equivalence has no provenance")
        _check_provenance(carbon.tree_year_provenance, "tree-year equivalence")

    for text, what in (
        (carbon.displaced_framing, "displaced-generation framing"),
        (carbon.cleaner_grid_statement, "cleaner-grid statement"),
    ):
        if not text.strip():
            raise ConfigError(f"{what} is empty")
