from datetime import date
from decimal import Decimal

from smartwatt_tariff.whatif import whatif

OUT_WINDOW = date(2027, 1, 15)
D = Decimal


def test_naive_saving_is_rate_times_kwh():
    """What every other calculator in Malaysia would tell you."""
    result = whatif(D("300"), D("20"), OUT_WINDOW)
    assert result.naive_saving == D("20") * D("25.0") / D(100)


def test_true_saving_mid_band_equals_naive():
    result = whatif(D("300"), D("20"), OUT_WINDOW)
    assert result.true_saving == result.naive_saving
    assert result.crosses_back is False


def test_crossing_back_under_a_boundary_is_worth_far_more():
    """The gap between naive and true is the product's whole argument."""
    result = whatif(D("417"), D("20"), OUT_WINDOW)
    assert result.crosses_back is True
    assert result.true_saving > result.naive_saving * 2


def test_the_worked_example_numbers():
    # 417 kWh at 29.5 sen = RM 123.015
    # 397 kWh at 27.0 sen = RM 107.190
    result = whatif(D("417"), D("20"), OUT_WINDOW)
    assert result.true_saving == D("123.015") - D("107.190")
    assert result.naive_saving == D("20") * D("0.295")


def test_multiple_reports_the_gap():
    result = whatif(D("417"), D("20"), OUT_WINDOW)
    assert result.multiple == result.true_saving / result.naive_saving
    assert result.multiple > 2


def test_multiple_is_one_when_no_boundary_is_crossed():
    assert whatif(D("300"), D("20"), OUT_WINDOW).multiple == D(1)


def test_saving_more_than_consumed_is_clamped_to_available():
    result = whatif(D("50"), D("200"), OUT_WINDOW)
    assert result.kwh_saved == D("50")


def test_zero_saving_is_zero():
    result = whatif(D("400"), D("0"), OUT_WINDOW)
    assert result.naive_saving == D(0)
    assert result.true_saving == D(0)
    assert result.multiple == D(1)


def test_every_field_is_decimal():
    result = whatif(D("417"), D("20"), OUT_WINDOW)
    for name in ("kwh_saved", "naive_saving", "true_saving", "multiple"):
        assert isinstance(getattr(result, name), Decimal), name


def test_minimum_charge_floors_the_true_saving():
    """Below the minimum charge, saving more changes nothing."""
    result = whatif(D("20"), D("15"), OUT_WINDOW)
    assert result.true_saving == D(0)
    # The nominal calculation still promises a saving that does not exist,
    # so the multiple is 0 - not 1, and not undefined. No band boundary is
    # involved: the minimum charge alone absorbs the whole saving.
    assert result.naive_saving == D("15") * D("0.18")
    assert result.multiple == D(0)
    assert result.crosses_back is False


def test_display_returns_strings():
    """Money crosses the wire as strings so the browser cannot reintroduce
    binary floating point."""
    result = whatif(D("417"), D("20"), OUT_WINDOW)
    for value in result.display().values():
        assert isinstance(value, str)


def test_display_rounds_known_values():
    # 417 kWh at 29.5 sen = RM 123.015; 397 kWh at 27.0 sen = RM 107.190;
    # true_saving = 15.825, exactly the ROUND_HALF_UP midpoint at 2dp.
    result = whatif(D("417"), D("20"), OUT_WINDOW)
    display = result.display()
    assert display["kwh_saved"] == "20.00"
    assert display["naive_saving"] == "5.90"
    assert display["true_saving"] == "15.83"
    assert display["multiple"] == "2.68"
    assert display["crosses_back"] == "True"
