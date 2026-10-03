import copy
from dataclasses import fields
from datetime import date
from decimal import Decimal

import pytest

from smartwatt_tariff.bill import (
    Bill,
    BillingPeriodError,
    evaluate,
    evaluate_undiscounted,
    rate_for,
)
from smartwatt_tariff.loader import load_tariff

# Inside the 25% state discount window (April-December 2026).
IN_WINDOW = date(2026, 8, 13)
# After it expires.
OUT_WINDOW = date(2027, 1, 15)

D = Decimal


@pytest.mark.parametrize(
    "kwh,expected",
    [
        ("1",    "18.0"),
        ("100",  "18.0"),
        ("101",  "18.0"),
        ("150",  "18.0"),
        ("151",  "22.0"),
        ("200",  "22.0"),
        ("300",  "25.0"),
        ("400",  "27.0"),
        ("400.5", "29.5"),
        ("401",  "29.5"),
        ("500",  "29.5"),
        ("700",  "30.0"),
        ("800",  "30.5"),
        ("1300", "31.0"),
        ("1301", "31.5"),
        ("99999", "31.5"),
    ],
)
def test_band_selection(kwh, expected):
    assert rate_for(D(kwh)) == D(expected)


def test_the_headline_cliff_undiscounted():
    """PRD's load-bearing arithmetic. 400 x 27.0 sen against 401 x 29.5 sen."""
    at_400 = evaluate(D("400"), OUT_WINDOW)
    at_401 = evaluate(D("401"), OUT_WINDOW)
    assert at_400.total == D("108.000")
    assert at_401.total == D("118.295")
    assert at_401.total - at_400.total == D("10.295")


def test_the_cliff_is_38x_nominal():
    at_400 = evaluate(D("400"), OUT_WINDOW)
    at_401 = evaluate(D("401"), OUT_WINDOW)
    marginal = at_401.total - at_400.total
    ratio = marginal / (D("27.0") / D(100))
    assert D("38.0") < ratio < D("38.2")


def test_the_cliff_under_discount():
    """Both sides scale by 0.75, so the ratio holds and the penalty is 7.72125."""
    at_400 = evaluate(D("400"), IN_WINDOW)
    at_401 = evaluate(D("401"), IN_WINDOW)
    assert at_401.total - at_400.total == D("7.72125")


def test_flat_band_not_progressive_block():
    """THE property that distinguishes the two tariff models. Under a
    progressive block tariff the 401st unit would cost 29.5 sen; under a
    flat-band tariff it re-prices the whole month."""
    at_400 = evaluate(D("400"), OUT_WINDOW)
    at_401 = evaluate(D("401"), OUT_WINDOW)
    one_unit_at_new_rate = D("29.5") / D(100)
    assert (at_401.total - at_400.total) > one_unit_at_new_rate * 30


def test_minimum_charge_floor():
    bill = evaluate(D("10"), OUT_WINDOW)
    assert bill.energy == D("5.00")
    assert bill.total == D("5.00")


def test_minimum_charge_does_not_bind_above_it():
    bill = evaluate(D("100"), OUT_WINDOW)
    assert bill.energy == D("18.0")


def test_no_service_tax_at_600():
    assert evaluate(D("600"), OUT_WINDOW).service_tax == D(0)


def test_service_tax_above_600():
    """8% on the units above 600, at the SELECTED band rate."""
    bill = evaluate(D("601"), OUT_WINDOW)
    assert bill.rate_sen == D("30.0")
    assert bill.service_tax == D("1") * D("0.30") * D("0.08")


def test_service_tax_uses_selected_rate_not_nominal():
    bill = evaluate(D("1000"), OUT_WINDOW)
    assert bill.rate_sen == D("31.0")
    assert bill.service_tax == D("400") * D("0.31") * D("0.08")


def test_discount_base_is_total_not_energy():
    """The draft's energy-only base was wrong. Regression-tested."""
    bill = evaluate(D("1000"), IN_WINDOW)
    assert bill.discount == (bill.energy + bill.service_tax) * D("0.25")
    assert bill.discount != bill.energy * D("0.25")


def test_discount_outside_window_is_zero():
    bill = evaluate(D("400"), OUT_WINDOW)
    assert bill.discount == D(0)
    assert bill.discounted is False


def test_discount_boundary_dates():
    assert evaluate(D("400"), date(2026, 3, 31)).discount == D(0)
    assert evaluate(D("400"), date(2026, 4, 1)).discount > D(0)
    assert evaluate(D("400"), date(2026, 12, 31)).discount > D(0)
    assert evaluate(D("400"), date(2027, 1, 1)).discount == D(0)


def test_discounted_is_exactly_three_quarters():
    discounted = evaluate(D("407"), IN_WINDOW)
    undiscounted = evaluate_undiscounted(D("407"), IN_WINDOW)
    assert discounted.total == undiscounted.total * D("0.75")


def test_undiscounted_ignores_the_window():
    assert evaluate_undiscounted(D("400"), IN_WINDOW).discount == D(0)


def test_every_money_field_is_decimal():
    bill = evaluate(D("407"), IN_WINDOW)
    for name in ("rate_sen", "energy", "service_tax", "discount", "total"):
        assert isinstance(getattr(bill, name), Decimal), name


def test_no_field_is_a_float():
    bill = evaluate(D("407"), IN_WINDOW)
    for f in fields(bill):
        assert not isinstance(getattr(bill, f.name), float), f.name


def test_display_rounds_to_two_places():
    bill = evaluate(D("401"), OUT_WINDOW)
    assert bill.display()["total"] == "118.30"


def test_display_returns_strings():
    """Money crosses the wire as strings so the browser cannot reintroduce
    binary floating point."""
    for value in evaluate(D("401"), OUT_WINDOW).display().values():
        assert isinstance(value, str)


def test_zero_consumption_is_the_minimum_charge():
    assert evaluate(D("0"), OUT_WINDOW).total == D("5.00")


def test_short_billing_period_raises():
    """The 28-day minimum never binds for a calendar month, but a caller
    passing something shorter gets an error rather than a figure whose
    service-tax semantics are unresolved."""
    with pytest.raises(BillingPeriodError):
        evaluate(D("400"), OUT_WINDOW, billing_days=14)


def test_billing_period_at_exactly_the_minimum_does_not_raise():
    """28 days is the configured minimum; only strictly below it should be
    rejected."""
    bill = evaluate(D("400"), OUT_WINDOW, billing_days=28)
    assert bill == evaluate(D("400"), OUT_WINDOW, billing_days=30)


def test_rate_override_bypasses_band_selection():
    """Used by cliff.band_step to price a consumption at a neighbouring rate."""
    bill = evaluate(D("400"), OUT_WINDOW, rate_override=D("29.5"))
    assert bill.rate_sen == D("29.5")
    assert bill.total == D("118.000")


@pytest.mark.parametrize("bad_rate", ["-5", "0"])
def test_rate_override_rejects_non_positive(bad_rate):
    """A non-positive override would otherwise produce a negative-energy
    bill instead of failing loudly."""
    with pytest.raises(ValueError, match=bad_rate):
        evaluate(D("400"), OUT_WINDOW, rate_override=D(bad_rate))


def test_unknown_discount_base_raises():
    """selftest.py constrains discount.base to 'total' or 'energy' before
    boot, but bill.py must not trust that it ran: an unrecognised base has
    to raise, not silently fall into the energy-only branch - that was the
    historical bug this whole module regression-tests against."""
    config = copy.deepcopy(load_tariff())
    object.__setattr__(config.discount, "base", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        evaluate(D("1000"), IN_WINDOW, config=config)


def test_explicit_config_overrides_the_shipped_one():
    """loader.resolve_tariff prefers a caller-supplied config to the shipped
    one; this reaches that branch from outside the package."""
    config = copy.deepcopy(load_tariff())
    object.__setattr__(config.bands[0], "sen_per_kwh", D("999.0"))
    bill = evaluate(D("50"), OUT_WINDOW, config=config)
    assert bill.rate_sen == D("999.0")


@pytest.mark.parametrize(
    "boundary", ["100", "150", "200", "300", "400", "500", "700", "800", "1300"]
)
def test_every_boundary_is_a_real_discontinuity_or_flat(boundary):
    b = D(boundary)
    below = evaluate(b, OUT_WINDOW)
    above = evaluate(b + D(1), OUT_WINDOW)
    if below.rate_sen == above.rate_sen:
        # The 100 -> 150 pair shares 18.0 sen; no cliff there.
        assert above.total - below.total == below.rate_sen / D(100)
    else:
        # Flat-band: crossing re-prices the entire month, so the jump is at
        # least the whole month re-priced. A progressive-block tariff would
        # charge only one unit at the new rate.
        repricing = b * (above.rate_sen - below.rate_sen) / D(100)
        assert above.total - below.total > repricing
