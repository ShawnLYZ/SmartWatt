import copy
from decimal import Decimal

import pytest

from smartwatt_tariff.loader import load_carbon, load_tariff
from smartwatt_tariff.selftest import ConfigError, run_selftest


def test_shipped_config_passes():
    run_selftest()


def test_ten_bands():
    assert len(load_tariff().bands) == 10


def test_first_band_is_the_1_to_100_band():
    """PRD correction: the draft omitted this band entirely."""
    first = load_tariff().bands[0]
    assert first.up_to_kwh == Decimal("100")
    assert first.sen_per_kwh == Decimal("18.0")


def test_final_band_is_open_ended():
    final = load_tariff().bands[-1]
    assert final.up_to_kwh is None
    assert final.sen_per_kwh == Decimal("31.5")


def test_the_400_and_500_bands():
    bands = {b.up_to_kwh: b.sen_per_kwh for b in load_tariff().bands}
    assert bands[Decimal("400")] == Decimal("27.0")
    assert bands[Decimal("500")] == Decimal("29.5")


def test_charges():
    charges = load_tariff().charges
    assert charges.minimum_monthly_rm == Decimal("5.00")
    assert charges.service_tax_rate == Decimal("0.08")
    assert charges.service_tax_exempt_kwh == Decimal("600")
    assert charges.minimum_billing_days == 28


def test_discount_base_is_total():
    """The draft's energy-only discount base was wrong."""
    assert load_tariff().discount.base == "total"
    assert load_tariff().discount.rate == Decimal("0.25")


def test_every_money_value_is_decimal():
    """A float in a bill path defeats the whole discipline."""
    config = load_tariff()
    for band in config.bands:
        assert isinstance(band.sen_per_kwh, Decimal)
        assert band.up_to_kwh is None or isinstance(band.up_to_kwh, Decimal)
    assert isinstance(config.charges.minimum_monthly_rm, Decimal)
    assert isinstance(config.discount.rate, Decimal)


def test_emission_factors():
    factors = {f.region: f.kg_per_kwh for f in load_carbon().factors}
    assert factors["sarawak"] == Decimal("0.199")
    assert factors["peninsular"] == Decimal("0.740")
    assert factors["sabah"] == Decimal("0.539")


def test_factors_are_marked_provisional():
    """US33: the system's confidence must match the source's."""
    for factor in load_carbon().factors:
        assert factor.provisional is True
        assert "provisional" in factor.provenance.vintage.lower()


def test_peninsular_trend_is_falling():
    """The transferability argument depreciates each year. Saying so is
    more credible than a round number that does not."""
    trend = load_carbon().peninsular_trend
    assert trend == {2022: Decimal("0.769"), 2023: Decimal("0.760"),
                     2024: Decimal("0.740")}


def test_displaced_framing_has_no_number():
    """PRD forbids inventing a marginal-intensity figure; the type system
    enforces it by not having a numeric field."""
    framing = load_carbon().displaced_framing
    assert isinstance(framing, str)
    assert framing.strip()


@pytest.mark.parametrize("field", ["source", "url", "vintage"])
def test_empty_band_provenance_fails(field):
    config = copy.deepcopy(load_tariff())
    object.__setattr__(config.provenance, field, "")
    with pytest.raises(ConfigError, match="provenance"):
        run_selftest(tariff=config)


@pytest.mark.parametrize("field", ["source", "url", "vintage"])
def test_empty_factor_provenance_fails(field):
    config = copy.deepcopy(load_carbon())
    object.__setattr__(config.factors[0].provenance, field, "")
    with pytest.raises(ConfigError, match="provenance"):
        run_selftest(carbon=config)


def test_band_gap_fails():
    config = copy.deepcopy(load_tariff())
    config.bands.pop(3)
    with pytest.raises(ConfigError, match="contiguous|ascending"):
        run_selftest(tariff=config)


def test_band_inversion_fails():
    config = copy.deepcopy(load_tariff())
    config.bands[2], config.bands[3] = config.bands[3], config.bands[2]
    with pytest.raises(ConfigError, match="ascending"):
        run_selftest(tariff=config)


def test_discount_window_backwards_fails():
    config = copy.deepcopy(load_tariff())
    object.__setattr__(
        config.discount, "effective_to", config.discount.effective_from
    )
    object.__setattr__(
        config.discount, "effective_from",
        config.discount.effective_from.replace(year=2027)
    )
    with pytest.raises(ConfigError, match="window"):
        run_selftest(tariff=config)


def test_tree_year_factor_is_optional():
    """No sourced factor means no equivalence - never an invented number."""
    carbon = load_carbon()
    assert carbon.tree_year_kg is None or isinstance(carbon.tree_year_kg, Decimal)


def test_no_open_ended_band_fails():
    config = copy.deepcopy(load_tariff())
    object.__setattr__(config.bands[-1], "up_to_kwh", Decimal("1400"))
    with pytest.raises(ConfigError, match="exactly one open-ended band"):
        run_selftest(tariff=config)


def test_band_non_positive_rate_fails():
    config = copy.deepcopy(load_tariff())
    object.__setattr__(config.bands[0], "sen_per_kwh", Decimal("0"))
    with pytest.raises(ConfigError, match="band rates must be positive"):
        run_selftest(tariff=config)


def test_discount_base_invalid_enum_fails():
    config = copy.deepcopy(load_tariff())
    object.__setattr__(config.discount, "base", "bogus")
    with pytest.raises(ConfigError, match="discount base must be"):
        run_selftest(tariff=config)


def test_discount_rate_out_of_range_fails():
    config = copy.deepcopy(load_tariff())
    object.__setattr__(config.discount, "rate", Decimal("1.5"))
    with pytest.raises(ConfigError, match="discount rate must be between 0 and 1"):
        run_selftest(tariff=config)


def test_no_factors_fails():
    config = copy.deepcopy(load_carbon())
    config.factors = []
    with pytest.raises(ConfigError, match="no emission factors configured"):
        run_selftest(carbon=config)


def test_factor_non_positive_rate_fails():
    config = copy.deepcopy(load_carbon())
    object.__setattr__(config.factors[0], "kg_per_kwh", Decimal("0"))
    with pytest.raises(ConfigError, match="emission factor 'sarawak' must be positive"):
        run_selftest(carbon=config)


def test_tree_year_without_provenance_fails():
    config = copy.deepcopy(load_carbon())
    config.tree_year_kg = Decimal("21.77")
    config.tree_year_provenance = None
    with pytest.raises(ConfigError, match="tree-year equivalence has no provenance"):
        run_selftest(carbon=config)


@pytest.mark.parametrize("field", ["displaced_framing", "cleaner_grid_statement"])
def test_empty_framing_text_fails(field):
    config = copy.deepcopy(load_carbon())
    object.__setattr__(config, field, "   ")
    with pytest.raises(ConfigError, match="is empty"):
        run_selftest(carbon=config)
