from unittest.mock import patch

import pytest

from smartwatt_server.registry import Device, Registry


@pytest.fixture
def registry(store):
    r = Registry(store)
    r.seed_defaults()
    return r


def test_seeds_the_five_trained_classes(registry):
    ids = {device.appliance_id for device in registry.all()}
    assert {"kettle", "desk_fan", "incandescent_lamp",
            "led_bulb", "laptop_charger"} <= ids


def test_laptop_charger_is_protected(registry):
    """A real device with a real reason to be protected, rather than a load
    standing in for a fridge - which removes a caveat that would otherwise
    need explaining every time."""
    assert registry.get("laptop_charger").protected is True


def test_kettle_is_heating(registry):
    assert registry.get("kettle").heating is True


def test_incandescent_lamp_is_neither(registry):
    lamp = registry.get("incandescent_lamp")
    assert lamp.protected is False
    assert lamp.heating is False


def test_plugs_are_registered(registry):
    assert registry.get("incandescent_lamp").plug_device is not None
    assert registry.get("kettle").plug_device is not None


def test_lookup_by_plug_device(registry):
    lamp = registry.get("incandescent_lamp")
    assert registry.by_plug(lamp.plug_device).appliance_id == "incandescent_lamp"


def test_unknown_appliance_is_none(registry):
    assert registry.get("nonexistent") is None


def test_residual_is_pseudo(registry):
    assert registry.is_pseudo("__residual__") is True


def test_unknown_n_is_pseudo(registry):
    assert registry.is_pseudo("unknown_1") is True
    assert registry.is_pseudo("unknown_42") is True


def test_real_appliance_is_not_pseudo(registry):
    assert registry.is_pseudo("kettle") is False


def test_pseudo_appliances_are_never_registered(registry):
    """They appear in the ledger and are structurally uncontrollable."""
    assert registry.get("__residual__") is None
    assert registry.get("unknown_1") is None


def test_seeding_is_idempotent(store):
    a, b = Registry(store), Registry(store)
    a.seed_defaults()
    before = len(a.all())
    b.seed_defaults()
    assert len(b.all()) == before


def test_registry_reads_from_the_store(store):
    Registry(store).seed_defaults()
    fresh = Registry(store)
    assert fresh.get("kettle") is not None


def test_laptop_charger_is_registered_with_a_plug(registry):
    """The laptop charger is the PROTECTED device, and the whole point of S7
    is that an evaluator can attempt to cut it and watch the gate refuse. The
    gate checks "registered with a plug" BEFORE it checks "protected", so a
    plugless laptop charger would be refused as unregistered and the protected
    guarantee could never be demonstrated."""
    assert registry.get("laptop_charger").plug_device is not None


def test_led_bulb_is_registered_with_a_plug(registry):
    """Task 4's standby_draw rule targets led_bulb, and warning about a device
    that carries no plug is an alarm with no possible resolution — the rule
    engine skips plugless devices, so the standby rule could never fire."""
    assert registry.get("led_bulb").plug_device is not None


def test_seeding_goes_through_locked_store_method(store):
    """Seed goes through Store.upsert_appliance(), which holds the write lock.
    This guards against a later refactor that might bypass the lock and
    reintroduce the concurrent-ingest hazard."""
    with patch.object(store, "upsert_appliance", wraps=store.upsert_appliance) as mock_upsert:
        registry = Registry(store)
        registry.seed_defaults()
        # Five devices seeded, each calls upsert_appliance once
        assert mock_upsert.call_count == 5
        # Verify the method was called with correct arguments for kettle
        kettle_call = [c for c in mock_upsert.call_args_list
                      if c.kwargs.get("appliance_id") == "kettle"][0]
        assert kettle_call.kwargs == {
            "appliance_id": "kettle",
            "display_name": "Kettle",
            "protected": False,
            "heating": True,
            "plug_device": "plug_kettle",
        }


# -- sync(): the household's own list, from appliances.toml --------------------


def _ids(registry):
    return sorted(device.appliance_id for device in registry.all())


def test_sync_installs_exactly_the_given_list(registry):
    """An appliance the owner deleted from appliances.toml must not survive
    in the registry: it would stay on the Control screen and stay switchable
    under the protected/heating flags it last had."""
    registry.sync((
        Device("rice_cooker", "Rice cooker", "plug_rice", protected=False, heating=True),
        Device("fridge", "Fridge", "plug_fridge", protected=True, heating=False),
    ))
    assert _ids(registry) == ["fridge", "rice_cooker"]
    assert registry.get("kettle") is None
    assert registry.get("fridge").protected is True
    assert registry.get("rice_cooker").heating is True
    assert registry.by_plug("plug_kettle") is None


def test_sync_lets_a_plug_move_to_a_new_appliance(registry):
    """plug_kettle belonged to the kettle. Given to a new appliance, an
    upsert would collide with the kettle's row on the unique plug index and
    the server would not start."""
    registry.sync((Device("heater", "Heater", "plug_kettle", protected=False, heating=True),))
    assert registry.by_plug("plug_kettle").appliance_id == "heater"


def test_sync_lets_two_appliances_swap_plugs(registry):
    registry.sync((
        Device("kettle", "Kettle", "plug_lamp", protected=False, heating=True),
        Device("incandescent_lamp", "Lamp", "plug_kettle", protected=False, heating=False),
    ))
    assert registry.by_plug("plug_lamp").appliance_id == "kettle"
    assert registry.by_plug("plug_kettle").appliance_id == "incandescent_lamp"


def test_sync_goes_through_the_locked_store_method(store):
    """Like seeding, sync reaches the database through one Store method
    that holds the write lock, never through the connection directly."""
    devices = (Device("fan", "Fan", None, protected=False, heating=False),)
    with patch.object(
        store, "replace_appliances", wraps=store.replace_appliances
    ) as replace:
        Registry(store).sync(devices)
    replace.assert_called_once_with([{
        "appliance_id": "fan", "display_name": "Fan",
        "protected": False, "heating": False, "plug_device": None,
    }])
