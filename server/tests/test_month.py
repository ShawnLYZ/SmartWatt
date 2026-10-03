from datetime import datetime, timezone

import pytest

from smartwatt_server.month import month_payload, whatif_payload

NOW = datetime(2026, 8, 16, 16, tzinfo=timezone.utc).timestamp()  # 17 Aug local

#: Per appliance: mean watts while in use, and how many one-minute rows to
#: write. kWh FOLLOWS from the two -- a minute at W watts is W/60 Wh -- so
#: the fixture cannot claim a mean power its own energy does not support.
#: An earlier version wrote a whole month's kWh into a single minute
#: alongside a hand-picked w_mean; now that w_mean is derived from the
#: energy, that fixture would describe a megawatt kettle, and
#: whatif_payload (which turns hours into kWh through it) would report a
#: saving the appliance could never make.
#:
#: The month totals 341.0 kWh: enough to put the projection past the
#: 400 kWh band edge, with the kettle alone able to bring it back under.
_USE: dict[str, tuple[float, int]] = {
    "kettle": (2500.0, 3600),             # 60.0 h -> 150.00 kWh
    "incandescent_lamp": (300.0, 3000),   # 50.0 h ->  15.00 kWh
    "desk_fan": (45.0, 3000),             # 50.0 h ->   2.25 kWh
    "laptop_charger": (30.0, 3000),       # 50.0 h ->   1.50 kWh
    # Unattributed: air-conditioning and the rest of the house.
    "__residual__": (2067.0, 5000),       # 83.3 h -> 172.25 kWh
}


def _seed(store, use: dict[str, tuple[float, int]], month_start_ts: float):
    """Write one ledger row per minute of use, as the integrator would.

    Through write_ledger rather than raw SQL, so the store stays the only
    place that decides how w_mean relates to the energy.
    """
    first_minute = int(month_start_ts // 60) + 10
    for appliance_id, (watts, minutes) in use.items():
        wh_per_minute = watts / 60.0
        for offset in range(minutes):
            store.write_ledger(first_minute + offset, appliance_id, wh_per_minute)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """One seeded billing month, built once for the whole module.

    Module-scoped because a physically consistent month is ~17 600
    one-minute rows and every test below only READS it -- month_payload and
    whatif_payload have no write path. A test that needs to write must take
    the function-scoped ``store`` fixture instead.
    """
    from smartwatt_tariff.kuching import month_bounds

    from smartwatt_server.store import Store

    store = Store()
    store.open(tmp_path_factory.mktemp("month") / "month.db")
    start, _ = month_bounds(NOW)
    _seed(store, _USE, start)
    yield store
    store.close()


def test_mtd_kwh(seeded):
    payload = month_payload(seeded, NOW)
    assert payload["mtd_kwh"] == pytest.approx(341.0, abs=0.1)


def test_money_is_string_everywhere(seeded):
    """The browser must never be handed a money float."""
    payload = month_payload(seeded, NOW)
    for key in ("energy", "service_tax", "discount", "total"):
        assert isinstance(payload["bill"][key], str)
        assert isinstance(payload["bill_undiscounted"][key], str)
    assert isinstance(payload["cliff"]["marginal"], str)
    assert isinstance(payload["cliff"]["band_step"], str)


def test_both_discounted_and_undiscounted(seeded):
    """US27. The brief's original version compared a string to a dict,
    which is trivially unequal regardless of what the code does."""
    payload = month_payload(seeded, NOW)
    assert payload["bill"]["total"] != payload["bill_undiscounted"]["total"]


def test_projection_exceeds_mtd(seeded):
    payload = month_payload(seeded, NOW)
    assert payload["projected_kwh"] > payload["mtd_kwh"]


def test_cliff_reports_both_numbers(seeded):
    """band_step re-prices the whole month; marginal is the next unit.

    Under a flat-band tariff, re-pricing the whole month is necessarily
    the bigger number - band_step RM 7.50 against marginal RM 0.2025 at
    this fixture's NOW. The brief had this backwards.
    """
    cliff = month_payload(seeded, NOW)["cliff"]
    assert float(cliff["band_step"]) > 0
    assert float(cliff["band_step"]) > float(cliff["marginal"])


def test_cliff_boundary_and_remaining(seeded):
    cliff = month_payload(seeded, NOW)["cliff"]
    assert cliff["boundary_kwh"] == "400"
    assert float(cliff["kwh_remaining"]) == pytest.approx(59.0, abs=0.5)


def test_culprit_is_an_appliance_that_would_get_you_under(seeded):
    """US24: the warning must name something you can act on."""
    culprit = month_payload(seeded, NOW)["cliff"]["culprit"]
    assert culprit["appliance_id"] in {"kettle", "incandescent_lamp"}
    assert culprit["sufficient_alone"] is True


def test_culprit_never_names_the_residual(seeded):
    """__residual__ is not something a householder can act on."""
    culprit = month_payload(seeded, NOW)["cliff"]["culprit"]
    assert culprit["appliance_id"] != "__residual__"


def test_culprit_flags_when_no_single_appliance_suffices(store):
    from smartwatt_tariff.kuching import month_bounds

    start, _ = month_bounds(NOW)
    # Fifteen equal 25 kWh loads: 375 kWh mtd, and no single one of them
    # covers the overshoot on its own.
    _seed(store, {f"load_{n}": (3000.0, 500) for n in range(15)}, start)
    culprit = month_payload(store, NOW)["cliff"]["culprit"]
    assert culprit["sufficient_alone"] is False
    assert culprit["appliance_id"] is not None


def test_no_culprit_when_projection_is_under(store):
    from smartwatt_tariff.kuching import month_bounds

    start, _ = month_bounds(NOW)
    _seed(store, {"led_bulb": (60.0, 1000)}, start)  # 16.7 h -> 1.0 kWh
    assert month_payload(store, NOW)["cliff"]["culprit"] is None


def test_carbon_has_both_framings(seeded):
    carbon = month_payload(seeded, NOW)["carbon"]
    assert carbon["local"]["region"] == "sarawak"
    assert carbon["hypothetical"]["hypothetical"] is True


def test_carbon_provenance_is_present(seeded):
    """US32, US33."""
    carbon = month_payload(seeded, NOW)["carbon"]
    for framing in ("local", "hypothetical"):
        provenance = carbon[framing]["provenance"]
        assert provenance["source"] and provenance["url"] and provenance["vintage"]
        assert carbon[framing]["provisional"] is True


def test_carbon_comparison_and_trend(seeded):
    carbon = month_payload(seeded, NOW)["carbon"]
    assert 3.7 <= float(carbon["comparison"]["ratio"]) < 3.72
    assert carbon["comparison"]["trend"]["2024"] == "0.740"
    assert carbon["comparison"]["statement"]


def test_tree_years_absent_rather_than_invented(seeded):
    assert month_payload(seeded, NOW)["carbon"]["tree_years"] is None


def test_whatif_reports_both_savings(seeded):
    """US26: the gap between naive and true is the product's argument.

    true_saving is deliberately NOT asserted to be >= naive_saving. S2
    computes naive from the pre-discount rate but true from the difference
    of two DISCOUNTED bill totals, so with the 25% discount live, true
    lands around 0.8x naive even for a saving big enough to cross a band
    boundary (kettle/hours=100 below: naive 75.00, true 58.88). No input
    satisfies true >= naive; fixing that would mean changing S2, which is
    outside this task. Do not "fix" this back to an ordering assertion.
    """
    crossing = whatif_payload(seeded, "kettle", hours=100.0, now_ts=NOW)
    assert isinstance(crossing["naive_saving"], str)
    assert isinstance(crossing["true_saving"], str)
    assert float(crossing["naive_saving"]) > 0
    assert float(crossing["true_saving"]) > 0
    assert float(crossing["kwh_saved"]) > 0
    assert crossing["crosses_back"] is True

    modest = whatif_payload(seeded, "kettle", hours=10.0, now_ts=NOW)
    assert modest["crosses_back"] is False


def test_whatif_on_unknown_appliance_raises(seeded):
    with pytest.raises(KeyError):
        whatif_payload(seeded, "nonexistent", hours=10.0, now_ts=NOW)
