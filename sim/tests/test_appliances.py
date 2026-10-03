import math

import pytest

from smartwatt_sim.appliances import (
    DEFAULT_APPLIANCES_PATH,
    Appliance,
    Harmonic,
    load_appliances,
)

TRAINED = {"kettle", "desk_fan", "incandescent_lamp", "led_bulb", "laptop_charger"}
UNTRAINED = {"phone_charger", "clothes_iron"}


@pytest.fixture(scope="module")
def appliances() -> dict[str, Appliance]:
    return load_appliances(DEFAULT_APPLIANCES_PATH)


def test_seven_modelled_loads(appliances):
    assert set(appliances) == TRAINED | UNTRAINED


def test_five_trained_two_untrained(appliances):
    assert {a.id for a in appliances.values() if a.trained} == TRAINED
    assert {a.id for a in appliances.values() if not a.trained} == UNTRAINED


def test_phone_charger_is_untrained(appliances):
    """Excluding it removes the LED-bulb/phone-charger confusion by design."""
    assert appliances["phone_charger"].trained is False


def test_kettle_is_heating(appliances):
    assert appliances["kettle"].heating is True


def test_laptop_charger_is_protected(appliances):
    """A real device with a real reason to be protected."""
    assert appliances["laptop_charger"].protected is True


def test_every_appliance_has_a_fundamental(appliances):
    for a in appliances.values():
        assert any(h.h == 1 for h in a.harmonics), a.id


def test_harmonic_orders_are_odd_and_ascending(appliances):
    for a in appliances.values():
        orders = [h.h for h in a.harmonics]
        assert orders == sorted(orders), a.id
        assert all(o % 2 == 1 for o in orders), a.id


def test_kettle_is_near_resistive(appliances):
    """A kettle is the phase reference used for S6 calibration; it must be clean."""
    fundamental = next(h for h in appliances["kettle"].harmonics if h.h == 1)
    assert abs(fundamental.phase) < 0.05


def test_kettle_draws_about_1800_w(appliances):
    fundamental = next(h for h in appliances["kettle"].harmonics if h.h == 1)
    watts = 240.0 * fundamental.i_rms * math.cos(fundamental.phase)
    assert 1750.0 < watts < 1850.0


def test_fan_and_lamp_are_close_in_watts(appliances):
    """The project's central discrimination claim: same wattage, different Q1."""
    def watts(appliance_id: str) -> float:
        h1 = next(h for h in appliances[appliance_id].harmonics if h.h == 1)
        return 240.0 * h1.i_rms * math.cos(h1.phase)

    assert abs(watts("desk_fan") - watts("incandescent_lamp")) < 10.0


def test_fan_is_inductive_and_lamp_is_not(appliances):
    fan = next(h for h in appliances["desk_fan"].harmonics if h.h == 1)
    lamp = next(h for h in appliances["incandescent_lamp"].harmonics if h.h == 1)
    assert fan.phase > 0.3
    assert abs(lamp.phase) < 0.05


def test_appliance_is_frozen(appliances):
    with pytest.raises(Exception):
        appliances["kettle"].id = "nope"


def test_harmonic_is_frozen():
    h = Harmonic(h=1, i_rms=1.0, phase=0.0)
    with pytest.raises(Exception):
        h.i_rms = 2.0
