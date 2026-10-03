import math

import pytest

from smartwatt_sim.aggregate import (
    aggregate,
    dist_from_spectrum,
    metrics_from_phasors,
    sum_phasors,
)
from smartwatt_sim.appliances import (
    DEFAULT_APPLIANCES_PATH,
    Appliance,
    Harmonic,
    load_appliances,
)

V = 240.0


@pytest.fixture(scope="module")
def appliances():
    return load_appliances(DEFAULT_APPLIANCES_PATH)


def _appliance(*harmonics: Harmonic) -> Appliance:
    return Appliance(
        id="probe", harmonics=harmonics, inrush_ratio=1.0,
        inrush_decay_cycles=1, settle_cycles=1,
        protected=False, heating=False, trained=True,
    )


def test_pure_resistive_has_zero_q1_and_zero_dist():
    a = _appliance(Harmonic(h=1, i_rms=5.0, phase=0.0))
    m = aggregate(V, [a])
    assert m.p == pytest.approx(1200.0)
    assert m.q1 == pytest.approx(0.0, abs=1e-9)
    assert m.dist == pytest.approx(0.0, abs=1e-9)
    assert m.pf_true == pytest.approx(1.0)


def test_quarter_turn_inductive_has_zero_p():
    a = _appliance(Harmonic(h=1, i_rms=2.0, phase=math.pi / 2))
    m = aggregate(V, [a])
    assert m.p == pytest.approx(0.0, abs=1e-9)
    assert m.q1 == pytest.approx(480.0)


def test_harmonics_carry_no_real_power_under_pure_voltage():
    """Orthogonality: a pure fundamental voltage extracts no power from harmonics."""
    a = _appliance(
        Harmonic(h=1, i_rms=1.0, phase=0.0),
        Harmonic(h=3, i_rms=0.5, phase=0.0),
    )
    m = aggregate(V, [a])
    assert m.p == pytest.approx(240.0)
    assert m.dist == pytest.approx(120.0)


def test_p_and_q1_superpose_across_parallel_loads():
    """The property PRD.md corrects the draft to obtain."""
    a = _appliance(Harmonic(h=1, i_rms=3.0, phase=0.3))
    b = _appliance(Harmonic(h=1, i_rms=2.0, phase=0.9))
    ma, mb, mab = aggregate(V, [a]), aggregate(V, [b]), aggregate(V, [a, b])
    assert mab.p == pytest.approx(ma.p + mb.p)
    assert mab.q1 == pytest.approx(ma.q1 + mb.q1)


def test_nonactive_power_does_not_superpose():
    """sqrt(S^2 - P^2) is what the draft used, and it is not additive."""
    a = _appliance(Harmonic(h=1, i_rms=1.0, phase=0.0), Harmonic(h=3, i_rms=0.8, phase=0.0))
    b = _appliance(Harmonic(h=1, i_rms=1.0, phase=0.0), Harmonic(h=3, i_rms=0.8, phase=math.pi))

    def nonactive(m):
        return math.sqrt(max(0.0, m.s**2 - m.p**2))

    ma, mb, mab = aggregate(V, [a]), aggregate(V, [b]), aggregate(V, [a, b])
    assert nonactive(mab) != pytest.approx(nonactive(ma) + nonactive(mb))
    assert mab.q1 == pytest.approx(ma.q1 + mb.q1)


@pytest.mark.parametrize(
    "ids",
    [
        ("kettle",),
        ("led_bulb",),
        ("desk_fan", "incandescent_lamp"),
        ("kettle", "laptop_charger", "led_bulb"),
        ("desk_fan", "led_bulb", "laptop_charger", "incandescent_lamp"),
    ],
)
def test_dist_cross_derivation(appliances, ids):
    """THE test that proves reactive power was separated from distortion.

    (1) D = sqrt(S^2 - P^2 - Q1^2)   from aggregate power quantities
    (2) D = V1 * sqrt(sum I_h^2)     from the harmonic spectrum directly

    These are algebraically equal only when Q1 is genuinely fundamental
    reactive power. A chain that conflates reactive and distortion power
    satisfies (1) trivially and fails (2).
    """
    active = [appliances[i] for i in ids]
    phasors = sum_phasors(active)
    from_powers = metrics_from_phasors(V, phasors).dist
    from_spectrum = dist_from_spectrum(V, phasors)
    assert from_powers == pytest.approx(from_spectrum, abs=1e-9)


def test_empty_active_set_is_all_zero():
    m = aggregate(V, [])
    assert (m.p, m.q1, m.dist, m.s, m.irms) == (0.0, 0.0, 0.0, 0.0, 0.0)
    assert m.pf_true == 1.0
    assert m.pf_disp == 1.0


def test_sum_phasors_adds_per_order():
    a = _appliance(Harmonic(h=1, i_rms=1.0, phase=0.0))
    b = _appliance(Harmonic(h=1, i_rms=1.0, phase=0.0))
    assert sum_phasors([a, b])[1] == pytest.approx(complex(2.0, 0.0))


def test_antiphase_harmonics_cancel():
    a = _appliance(Harmonic(h=3, i_rms=1.0, phase=0.0))
    b = _appliance(Harmonic(h=3, i_rms=1.0, phase=math.pi))
    assert abs(sum_phasors([a, b])[3]) == pytest.approx(0.0, abs=1e-12)


def test_crest_of_a_sinusoid_is_root_two():
    a = _appliance(Harmonic(h=1, i_rms=1.0, phase=0.0))
    assert aggregate(V, [a]).crest == pytest.approx(math.sqrt(2), abs=1e-3)


def test_switch_mode_load_has_high_crest(appliances):
    assert aggregate(V, [appliances["led_bulb"]]).crest > 1.6


def test_pf_disp_and_pf_true_differ_for_distorting_load(appliances):
    m = aggregate(V, [appliances["led_bulb"]])
    assert m.pf_disp > m.pf_true
