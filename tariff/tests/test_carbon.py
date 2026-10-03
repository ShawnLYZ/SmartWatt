import copy
from decimal import Decimal
from typing import get_type_hints

import pytest

from smartwatt_tariff.carbon import (
    comparison,
    factors,
    hypothetical,
    local,
    tree_years,
)
from smartwatt_tariff.loader import load_carbon

D = Decimal


def test_local_uses_sarawaks_factor():
    result = local(D("407"))
    assert result.region == "sarawak"
    assert result.kg == D("407") * D("0.199")
    assert result.hypothetical is False


def test_hypothetical_is_labelled():
    """US31: always labelled hypothetical, never presented as this household's."""
    result = hypothetical(D("407"), "peninsular")
    assert result.hypothetical is True
    assert result.kg == D("407") * D("0.740")


def test_local_cannot_be_hypothetical():
    assert local(D("407")).hypothetical is False


def test_sabah_is_available():
    assert hypothetical(D("100"), "sabah").kg == D("100") * D("0.539")


def test_unknown_region_raises():
    with pytest.raises(KeyError):
        hypothetical(D("100"), "singapore")


def test_comparison_ratio_is_3_7():
    ratio = comparison().ratio
    assert D("3.7") < ratio < D("3.72")


def test_comparison_carries_the_trend():
    """The transferability argument depreciates each year, and saying so is
    more credible than a round number that does not."""
    trend = comparison().trend
    assert trend[2022] > trend[2023] > trend[2024]


def test_comparison_states_the_grid_is_already_cleaner():
    """US35: shows the project is not inflating its impact."""
    statement = comparison().statement.lower()
    assert "cleaner" in statement
    assert "0.199" in statement and "0.740" in statement


def test_displaced_framing_is_qualitative():
    """PRD forbids inventing a marginal-intensity number."""
    framing = comparison().displaced_framing
    assert isinstance(framing, str)
    assert "does not publish" in framing or "not have access" in framing


def test_comparison_has_no_numeric_displaced_field():
    from smartwatt_tariff.carbon import Comparison

    hints = get_type_hints(Comparison)
    assert hints["displaced_framing"] is str


def test_every_factor_carries_provenance():
    """US32: source, URL and vintage visible on screen."""
    for factor in factors():
        assert factor.provenance.source
        assert factor.provenance.url
        assert factor.provenance.vintage


def test_every_factor_is_marked_provisional():
    """US33: the system's confidence matches the source's."""
    assert all(f.provisional for f in factors())


def test_tree_years_is_none_when_unsourced():
    """No sourced factor, no equivalence. No figure is invented to fill it,
    and the server still boots."""
    assert tree_years(D("100")) is None


def test_tree_years_computes_when_sourced():
    config = copy.deepcopy(load_carbon())
    config.tree_year_kg = D("21.77")
    assert tree_years(D("100"), config=config) == D("100") / D("21.77")


def test_tree_years_of_zero():
    config = copy.deepcopy(load_carbon())
    config.tree_year_kg = D("21.77")
    assert tree_years(D("0"), config=config) == D(0)


def test_all_values_are_decimal():
    result = local(D("407"))
    assert isinstance(result.kg, Decimal)
    assert isinstance(result.factor.kg_per_kwh, Decimal)


def test_local_and_hypothetical_are_never_summed():
    """A test of intent: they are separate objects with a distinguishing
    flag, so a caller cannot accidentally add them without noticing."""
    a, b = local(D("100")), hypothetical(D("100"), "peninsular")
    assert a.hypothetical != b.hypothetical
    assert a.region != b.region


def test_display_returns_strings():
    """Money crosses the wire as strings so the browser cannot reintroduce
    binary floating point."""
    for value in local(D("407")).display().values():
        assert isinstance(value, str)


def test_display_rounds_known_value():
    # 407 kWh x 0.199 kg/kWh = 80.993 kg.
    display = local(D("407")).display()
    assert display["kg"] == "80.99"
    assert display["region"] == "sarawak"
    assert display["hypothetical"] == "False"


def test_display_of_hypothetical():
    # 100 kWh x 0.740 kg/kWh = 74.000 kg.
    display = hypothetical(D("100"), "peninsular").display()
    assert display["kg"] == "74.00"
    assert display["region"] == "peninsular"
    assert display["hypothetical"] == "True"
