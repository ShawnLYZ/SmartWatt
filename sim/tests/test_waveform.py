import json
import math

import pytest

from smartwatt_sim.appliances import DEFAULT_APPLIANCES_PATH, load_appliances
from smartwatt_sim.waveform import (
    CALIBRATION,
    SAMPLE_RATE_HZ,
    render,
    write_fixture,
)

V = 240.0
F = 50.0


@pytest.fixture(scope="module")
def appliances():
    return load_appliances(DEFAULT_APPLIANCES_PATH)


def test_eighty_samples_per_cycle():
    samples, _ = render([], V, F, cycles=1)
    assert len(samples) == 80
    assert SAMPLE_RATE_HZ / F == 80


def test_samples_are_integers_in_adc_range():
    samples, _ = render([], V, F, cycles=2)
    for v, i_low, i_high in samples:
        for value in (v, i_low, i_high):
            assert isinstance(value, int)
            assert 0 <= value <= CALIBRATION["adc_max"]


def test_idle_sits_at_bias():
    samples, _ = render([], V, F, cycles=1)
    assert all(i_low == CALIBRATION["adc_mid"] for _, i_low, _ in samples)


def test_voltage_peak_matches_v_cal():
    samples, _ = render([], V, F, cycles=1)
    peak = max(abs(v - CALIBRATION["adc_mid"]) for v, _, _ in samples)
    expected = V * math.sqrt(2) / CALIBRATION["v_cal"]
    assert peak == pytest.approx(expected, rel=0.02)


def test_260_v_fits_without_clipping():
    """Master spec headroom check: 260 V rms must land under 4095 counts."""
    samples, _ = render([], 260.0, F, cycles=1)
    assert max(v for v, _, _ in samples) < CALIBRATION["adc_max"]


def test_led_bulb_lands_near_26_counts(appliances):
    """The first hardware milestone (S6, assumption A2), checked in simulation."""
    samples, _ = render([appliances["led_bulb"]], V, F, cycles=2)
    peak = max(abs(i - CALIBRATION["adc_mid"]) for _, i, _ in samples)
    assert 20 <= peak <= 60


def test_sidecar_carries_analytic_values(appliances):
    _, sidecar = render([appliances["kettle"]], V, F, cycles=4)
    for key in ("p", "q1", "dist", "s", "irms", "pf_true", "pf_disp",
                "freq", "v_rms", "harmonic_rms", "dist_from_spectrum"):
        assert key in sidecar


def test_sidecar_dist_cross_derivation(appliances):
    """Both routes to D must agree in every pure-V fixture."""
    for ids in (("kettle",), ("led_bulb",), ("desk_fan", "led_bulb"),
                ("kettle", "laptop_charger", "incandescent_lamp")):
        active = [appliances[i] for i in ids]
        _, sidecar = render(active, V, F, cycles=4)
        assert sidecar["dist"] == pytest.approx(sidecar["dist_from_spectrum"], abs=1e-9)
        assert sidecar["cross_derivation_holds"] is True


def test_distorted_voltage_breaks_the_shortcut(appliances):
    """The inverse fixture: proves the chain uses the general formula for D."""
    _, sidecar = render([appliances["led_bulb"]], V, F, cycles=4, distorted_v=True)
    assert sidecar["cross_derivation_holds"] is False
    assert sidecar["dist_from_spectrum"] is None


def test_high_range_is_scaled_down_from_low(appliances):
    samples, _ = render([appliances["kettle"]], V, F, cycles=1)
    ratio = CALIBRATION["i_cal_high"] / CALIBRATION["i_cal_low"]
    lows = [i_low - CALIBRATION["adc_mid"] for _, i_low, _ in samples]
    highs = [i_high - CALIBRATION["adc_mid"] for _, _, i_high in samples]
    unclipped = [
        n for n in range(len(lows))
        if 0 < samples[n][1] < CALIBRATION["adc_max"]
    ]
    biggest = max(unclipped, key=lambda n: abs(highs[n]))
    assert lows[biggest] == pytest.approx(highs[biggest] * ratio, rel=0.05)


def test_kettle_clips_the_low_range(appliances):
    """A 1800 W load must saturate i_low - that is why i_high exists."""
    samples, _ = render([appliances["kettle"]], V, F, cycles=1)
    assert any(i_low in (0, CALIBRATION["adc_max"]) for _, i_low, _ in samples)
    assert all(0 < i_high < CALIBRATION["adc_max"] for _, _, i_high in samples)


def test_write_fixture_produces_both_files(tmp_path, appliances):
    csv_path, sidecar_path = write_fixture(
        "pure-resistive", [appliances["kettle"]], tmp_path, v_rms=V, freq=F, cycles=4
    )
    assert csv_path.exists() and sidecar_path.exists()
    header, *rows = csv_path.read_text().strip().splitlines()
    assert header == "v,i_low,i_high"
    assert len(rows) == 320
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["name"] == "pure-resistive"
    assert sidecar["sample_rate_hz"] == SAMPLE_RATE_HZ


def test_frequency_is_recorded_exactly(appliances):
    _, sidecar = render([appliances["kettle"]], V, 49.7, cycles=2)
    assert sidecar["freq"] == pytest.approx(49.7)
