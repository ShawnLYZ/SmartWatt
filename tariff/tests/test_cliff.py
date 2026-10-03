import copy
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from smartwatt_tariff.cliff import band_step, marginal, next_boundary, project
from smartwatt_tariff.loader import load_tariff

OUT_WINDOW = date(2027, 1, 15)
IN_WINDOW = date(2026, 8, 13)
D = Decimal


def _utc(year, month, day, hour=0) -> float:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


def test_marginal_is_the_headline_number():
    assert marginal(D("400"), OUT_WINDOW) == D("10.295")


def test_marginal_under_discount():
    assert marginal(D("400"), IN_WINDOW) == D("7.72125")


def test_band_step_is_the_repricing_alone():
    """US22: it re-prices the entire month, not just the next unit.
    400 x 29.5 sen - 400 x 27.0 sen = RM 10.00."""
    assert band_step(D("400"), OUT_WINDOW) == D("10.000")


def test_band_step_and_marginal_are_different_numbers():
    """They answer different questions and S4 shows both."""
    step = band_step(D("400"), OUT_WINDOW)
    marg = marginal(D("400"), OUT_WINDOW)
    assert marg > step
    assert marg - step == D("29.5") / D(100)


def test_marginal_is_two_full_evaluations_not_rate_times_delta():
    """The rule that makes the band effect appear at all."""
    naive = D("27.0") / D(100)
    assert marginal(D("400"), OUT_WINDOW) != naive
    assert marginal(D("399"), OUT_WINDOW) == naive


def test_marginal_mid_band_is_just_the_rate():
    assert marginal(D("250"), OUT_WINDOW) == D("25.0") / D(100)


def test_marginal_accepts_a_delta():
    assert marginal(D("250"), OUT_WINDOW, delta=D("10")) == D("10") * D("0.25")


@pytest.mark.parametrize(
    "boundary,current,next_rate",
    [
        ("150", "18.0", "22.0"),
        ("200", "22.0", "25.0"),
        ("300", "25.0", "27.0"),
        ("400", "27.0", "29.5"),
        ("500", "29.5", "30.0"),
        ("700", "30.0", "30.5"),
        ("800", "30.5", "31.0"),
        ("1300", "31.0", "31.5"),
    ],
)
def test_band_step_at_every_real_boundary(boundary, current, next_rate):
    b = D(boundary)
    # Re-pricing the whole month moves the service tax too: tax is
    # (kwh - 600) x rate x 8%, and the rate is exactly what changed. An
    # energy-only expectation is short by that term above 600 kWh.
    step = (D(next_rate) - D(current)) / D(100)
    taxable = max(D(0), b - D("600"))
    expected = b * step + taxable * step * D("0.08")
    assert band_step(b, OUT_WINDOW) == expected


def test_the_100_to_150_boundary_has_no_step():
    """Both bands are 18.0 sen, so no arithmetic impact - but the 1-100 band
    belongs in the config, which is why it is there."""
    assert band_step(D("100"), OUT_WINDOW) == D(0)


def test_next_boundary_below_a_cliff():
    boundary = next_boundary(D("341"))
    assert boundary.up_to_kwh == D("400")
    assert boundary.kwh_remaining == D("59")
    assert boundary.current_rate_sen == D("27.0")
    assert boundary.next_rate_sen == D("29.5")


def test_next_boundary_exactly_at_one():
    boundary = next_boundary(D("400"))
    assert boundary.up_to_kwh == D("400")
    assert boundary.kwh_remaining == D("0")


def test_next_boundary_in_the_open_band_is_none():
    assert next_boundary(D("2000")) is None


def test_explicit_config_overrides_the_shipped_one():
    """loader.resolve_tariff prefers a caller-supplied config to the shipped
    one; this reaches that branch from outside the package."""
    config = copy.deepcopy(load_tariff())
    for band in config.bands:
        if band.up_to_kwh == D("400"):
            object.__setattr__(band, "sen_per_kwh", D("999.0"))
            break
    boundary = next_boundary(D("341"), config=config)
    assert boundary.current_rate_sen == D("999.0")


def test_projection_halfway_through_the_month():
    """Linear run-rate. Deliberately simple, labelled a projection on screen.
    Anything cleverer would be a model presented as a measurement."""
    # 16 days into a 31-day August (local midnight on the 17th).
    now = _utc(2026, 8, 16, 16)  # 00:00 on the 17th in Kuching
    projected = project(D("200"), now)
    assert projected == pytest.approx(D("200") * D("31") / D("16"), abs=D("0.5"))


def test_projection_at_month_end_equals_consumption():
    now = _utc(2026, 8, 31, 15, )  # 23:00 on the 31st in Kuching
    projected = project(D("407"), now)
    assert projected == pytest.approx(D("407"), abs=D("1"))


def test_projection_at_month_start_does_not_divide_by_zero():
    now = _utc(2026, 7, 31, 16)  # exactly local midnight on 1 August
    assert project(D("0"), now) >= D(0)


def test_projection_returns_decimal():
    assert isinstance(project(D("200"), _utc(2026, 8, 16, 16)), Decimal)


def test_projection_crossing_the_cliff_is_detectable():
    """The scenario S4's Cliff Gauge renders: under the boundary today,
    over it by month end."""
    now = _utc(2026, 8, 16, 16)
    mtd = D("341")
    projected = project(mtd, now)
    assert mtd < D("400") < projected
