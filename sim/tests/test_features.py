import math

import pytest

from smartwatt_sim.aggregate import aggregate
from smartwatt_sim.appliances import DEFAULT_APPLIANCES_PATH, load_appliances
from smartwatt_sim.features import FEATURE_NAMES, derive_features

V = 240.0


@pytest.fixture(scope="module")
def appliances():
    return load_appliances(DEFAULT_APPLIANCES_PATH)


def test_exactly_fourteen_names():
    assert len(FEATURE_NAMES) == 14
    assert len(set(FEATURE_NAMES)) == 14


def test_names_match_the_contract():
    assert FEATURE_NAMES == (
        "delta_p", "delta_q1", "delta_dist", "delta_s",
        "pf_disp", "pf_true", "delta_irms", "delta_crest",
        "h3_h1", "h5_h1", "h7_h1",
        "inrush_ratio", "settle_cycles", "log_delta_p",
    )


def test_returns_every_feature(appliances):
    kettle = appliances["kettle"]
    f = derive_features(V, before=[], after=[kettle], switched=kettle)
    assert set(f) == set(FEATURE_NAMES)


def test_on_and_off_produce_identical_features(appliances):
    """Magnitudes on all deltas, so ON and OFF share one feature space."""
    kettle = appliances["kettle"]
    on = derive_features(V, before=[], after=[kettle], switched=kettle)
    off = derive_features(V, before=[kettle], after=[], switched=kettle)
    for name in FEATURE_NAMES:
        assert on[name] == pytest.approx(off[name]), name


def test_all_deltas_are_non_negative(appliances):
    kettle = appliances["kettle"]
    f = derive_features(V, before=[kettle], after=[], switched=kettle)
    for name in ("delta_p", "delta_q1", "delta_dist", "delta_s",
                 "delta_irms", "delta_crest"):
        assert f[name] >= 0.0, name


def test_delta_p_matches_the_aggregate_difference(appliances):
    """Features are DERIVED from the phasor delta, never looked up."""
    kettle, fan = appliances["kettle"], appliances["desk_fan"]
    before, after = [fan], [fan, kettle]
    expected = abs(aggregate(V, after).p - aggregate(V, before).p)
    f = derive_features(V, before=before, after=after, switched=kettle)
    assert f["delta_p"] == pytest.approx(expected)


def test_delta_is_independent_of_background(appliances):
    """P and Q1 superpose, so switching one load gives the same delta
    whatever else is running. This is the property the tracker relies on."""
    kettle = appliances["kettle"]
    quiet = derive_features(V, before=[], after=[kettle], switched=kettle)
    busy_before = [appliances["desk_fan"], appliances["incandescent_lamp"]]
    busy = derive_features(
        V, before=busy_before, after=[*busy_before, kettle], switched=kettle
    )
    assert busy["delta_p"] == pytest.approx(quiet["delta_p"])
    assert busy["delta_q1"] == pytest.approx(quiet["delta_q1"])


def test_log_delta_p_is_ln_one_plus(appliances):
    kettle = appliances["kettle"]
    f = derive_features(V, before=[], after=[kettle], switched=kettle)
    assert f["log_delta_p"] == pytest.approx(math.log1p(f["delta_p"]))


def test_harmonic_ratios_come_from_the_switched_load(appliances):
    led = appliances["led_bulb"]
    f = derive_features(V, before=[], after=[led], switched=led)
    i1 = next(h.i_rms for h in led.harmonics if h.h == 1)
    i3 = next(h.i_rms for h in led.harmonics if h.h == 3)
    assert f["h3_h1"] == pytest.approx(i3 / i1)


def test_inrush_and_settle_come_from_the_appliance(appliances):
    lamp = appliances["incandescent_lamp"]
    f = derive_features(V, before=[], after=[lamp], switched=lamp)
    assert f["inrush_ratio"] == pytest.approx(lamp.inrush_ratio)
    assert f["settle_cycles"] == lamp.settle_cycles


def test_fan_and_lamp_separate_on_q1_and_inrush(appliances):
    """The project's central discrimination claim, in feature space."""
    fan, lamp = appliances["desk_fan"], appliances["incandescent_lamp"]
    ff = derive_features(V, before=[], after=[fan], switched=fan)
    lf = derive_features(V, before=[], after=[lamp], switched=lamp)
    assert abs(ff["delta_p"] - lf["delta_p"]) < 10.0
    assert ff["delta_q1"] > lf["delta_q1"] * 5
    assert lf["inrush_ratio"] > ff["inrush_ratio"]


def test_pf_bounds(appliances):
    for appliance in appliances.values():
        f = derive_features(V, before=[], after=[appliance], switched=appliance)
        assert -1.0 <= f["pf_disp"] <= 1.0
        assert -1.0 <= f["pf_true"] <= 1.0


def test_pf_true_is_clamped_with_a_busy_background(appliances):
    """delta_s does not superpose, so raw delta_p/delta_s can exceed 1."""
    kettle = appliances["kettle"]
    background = [appliances["desk_fan"], appliances["incandescent_lamp"]]
    f = derive_features(
        V, before=background, after=[*background, kettle], switched=kettle
    )
    assert f["pf_true"] == 1.0
    assert f["delta_p"] > f["delta_s"]   # the clamp is load-bearing here


def test_no_edge_direction_in_the_feature_space():
    """The classifier must be structurally incapable of seeing edge direction."""
    import inspect

    from smartwatt_sim import features

    assert "edge" not in inspect.signature(features.derive_features).parameters
    assert "edge" not in FEATURE_NAMES
