from decimal import Decimal

from smartwatt_tariff.display import SEN_PER_RINGGIT, to_2dp

D = Decimal


def test_to_2dp_returns_a_string():
    assert isinstance(to_2dp(D("1")), str)


def test_to_2dp_rounds_half_up_at_the_exact_midpoint():
    """The rounding mode's whole point: a trailing 5 at the third place
    rounds away from zero, not to even. Pinned explicitly so it cannot
    silently drift to ROUND_HALF_EVEN."""
    assert to_2dp(D("118.295")) == "118.30"


def test_to_2dp_rounds_down_below_the_midpoint():
    assert to_2dp(D("118.294")) == "118.29"


def test_to_2dp_pads_to_two_places():
    assert to_2dp(D("5")) == "5.00"


def test_to_2dp_handles_zero():
    assert to_2dp(D("0")) == "0.00"


def test_sen_per_ringgit_is_100():
    assert SEN_PER_RINGGIT == D(100)
