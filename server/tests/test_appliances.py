"""appliances.toml: the household's own list, and the one file a person
setting SmartWatt up is told to edit.

Two kinds of test. The loader's: what a correct file becomes, and that every
mistake a first-time editor is likely to make is refused with a message
naming the appliance and the fix. And the server's: that the list, not the
built-in example household, is what reaches the registry, the rules and the
wizard, and that a broken list stops the server rather than half-loading.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from smartwatt_server import rules, wizard
from smartwatt_server.api import create_app
from smartwatt_server.appliances import ApplianceFileError, load_appliances
from smartwatt_server.config import DEFAULT_APPLIANCES
from smartwatt_server.config import settings as load_settings
from smartwatt_server.registry import _DEFAULTS, Device

from .support import EXAMPLE_APPLIANCES

FAN = """
[[appliance]]
id = "fan"
name = "Fan"
protected = false
heating = false
"""


def _write(tmp_path, text, name="appliances.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _refusal(tmp_path, text):
    with pytest.raises(ApplianceFileError) as caught:
        load_appliances(_write(tmp_path, text))
    return str(caught.value)


# -- what a correct file becomes ----------------------------------------------


def test_the_example_file_is_the_built_in_example_household():
    """conftest.py runs every server test against this file, and the unit
    tests that call seed_defaults() run against the Python definitions. If
    the two drifted apart, the suite would test two different households."""
    catalogue = load_appliances(EXAMPLE_APPLIANCES)
    assert catalogue.devices == _DEFAULTS
    # Order matters here: it is the order the Setup screen lists them in.
    assert catalogue.trained == wizard.TRAINED_CLASSES
    # And not here: each target is evaluated on its own. The file lists
    # appliances in registry order, the rules module in its own.
    assert set(catalogue.left_on_targets) == set(rules.LEFT_ON_TARGETS)
    assert set(catalogue.standby_targets) == set(rules.STANDBY_TARGETS)


def test_the_repositorys_own_appliance_list_loads():
    """Whatever the root appliances.toml holds -- the shipped examples or
    somebody's own appliances -- the server must be able to start on it."""
    assert load_appliances(DEFAULT_APPLIANCES).devices


def test_optional_settings_take_their_defaults(tmp_path):
    """plug, train and the two rules may be left out: an appliance with no
    plug, trained, and watched by no rule."""
    catalogue = load_appliances(_write(tmp_path, FAN))
    assert catalogue.devices == (
        Device("fan", "Fan", None, protected=False, heating=False),
    )
    assert catalogue.trained == ("fan",)
    assert catalogue.left_on_targets == ()
    assert catalogue.standby_targets == ()


def test_an_empty_plug_means_no_plug(tmp_path):
    catalogue = load_appliances(_write(tmp_path, FAN + 'plug = ""\n'))
    assert catalogue.devices[0].plug_device is None


def test_flags_and_order_are_carried_through(tmp_path):
    text = """
[[appliance]]
id = "heater"
name = "  Heater  "
plug = "plug_heater"
protected = false
heating = true
train = false
left_on_rule = true
standby_rule = false

[[appliance]]
id = "fridge"
name = "Fridge"
plug = "plug_fridge"
protected = true
heating = false
train = true
"""
    catalogue = load_appliances(_write(tmp_path, text))
    assert catalogue.devices == (
        Device("heater", "Heater", "plug_heater", protected=False, heating=True),
        Device("fridge", "Fridge", "plug_fridge", protected=True, heating=False),
    )
    assert catalogue.trained == ("fridge",)
    assert catalogue.left_on_targets == ("heater",)
    assert catalogue.standby_targets == ()


def test_a_byte_order_mark_is_accepted(tmp_path):
    """Some Windows editors start a UTF-8 file with one, and the TOML parser
    alone would call it a syntax error on line 1."""
    path = tmp_path / "appliances.toml"
    path.write_bytes(b"\xef\xbb\xbf" + FAN.encode())
    assert load_appliances(path).devices[0].appliance_id == "fan"


# -- the mistakes a first-time editor makes -----------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(FAN.replace('"Fan"', "Fan"), "typing mistake", id="unquoted-text"),
        pytest.param(FAN.replace("= false", "= False", 1), "typing mistake",
                     id="capitalised-false"),
        pytest.param(FAN.replace("protected = false\n", ""),
                     'missing its "protected" line', id="missing-protected"),
        pytest.param(FAN.replace("heating = false\n", ""),
                     'missing its "heating" line', id="missing-heating"),
        pytest.param(FAN.replace('id = "fan"\n', ""), 'missing its "id" line',
                     id="missing-id"),
        pytest.param(FAN.replace("protected", "protect"), 'Did you mean "protected"',
                     id="misspelt-key"),
        pytest.param(FAN.replace("heating = false", 'heating = "false"'),
                     "must be  true  or  false", id="quoted-flag"),
        pytest.param(FAN.replace('"fan"', '"Rice Cooker"'), "lowercase letters",
                     id="id-with-capitals-and-space"),
        pytest.param(FAN.replace('"fan"', '"a' + "b" * 23 + '"'), "at most 23",
                     id="id-too-long"),
        pytest.param(FAN.replace('"fan"', '"unknown_3"'), "reserved",
                     id="id-reserved-for-untrained-loads"),
        pytest.param(FAN.replace('"Fan"', '"  "'), "name must be text",
                     id="blank-name"),
        pytest.param(FAN + 'plug = "plug/fan"\n', "Tasmota Topic", id="slash-in-plug"),
        pytest.param(FAN + 'plug = "plug fan"\n', "Tasmota Topic", id="space-in-plug"),
        pytest.param(FAN + "plug = 7\n", "plug must be text", id="plug-not-text"),
        pytest.param(FAN.replace("protected = false", "protected = true")
                     + "left_on_rule = true\n", "is protected", id="protected-with-rule"),
        pytest.param(FAN + FAN, 'The id "fan" is used 2 times', id="duplicate-id"),
        pytest.param(
            FAN + 'plug = "plug_a"\n' + FAN.replace('"fan"', '"tv"') + 'plug = "plug_a"\n',
            'both use plug "plug_a"', id="duplicate-plug"),
        pytest.param(FAN.replace("[[appliance]]", "[appliance]"), "TWO square brackets",
                     id="single-brackets"),
        pytest.param(FAN.replace("[[appliance]]", "[[appliances]]"),
                     'Did you mean "appliance"', id="misspelt-section"),
        pytest.param("# nothing here yet\n", "lists no appliances", id="empty-file"),
    ],
)
def test_a_mistake_is_refused_with_a_message_that_says_how_to_fix_it(
    tmp_path, text, expected
):
    assert expected in _refusal(tmp_path, text)


def test_every_problem_is_reported_at_once(tmp_path):
    """One restart per typo is a miserable way to fix a file."""
    text = FAN.replace("protected = false\n", "") + FAN.replace('"fan"', '"TV"')
    message = _refusal(tmp_path, text)
    assert "2 problems" in message
    assert 'appliance #1 ("fan") is missing its "protected" line' in message
    assert 'appliance #2 ("TV")' in message


def test_a_missing_file_is_refused_by_name(tmp_path):
    with pytest.raises(ApplianceFileError, match="does not exist"):
        load_appliances(tmp_path / "nowhere.toml")


def test_a_file_not_saved_as_utf8_is_refused_with_the_fix(tmp_path):
    path = tmp_path / "appliances.toml"
    path.write_text(FAN, encoding="utf-16")
    with pytest.raises(ApplianceFileError, match="UTF-8"):
        load_appliances(path)


# -- the server takes its household from the file -----------------------------

HOUSEHOLD = """
[[appliance]]
id = "rice_cooker"
name = "Rice cooker"
plug = "plug_rice"
protected = false
heating = true
left_on_rule = true

[[appliance]]
id = "fridge"
name = "Fridge"
plug = "plug_fridge"
protected = true
heating = false
train = false

[[appliance]]
id = "tv"
name = "Television"
protected = false
heating = false
standby_rule = true
"""


def _app(store, tmp_path, text, **overrides):
    config = dataclasses.replace(
        load_settings(),
        appliances_path=_write(tmp_path, text),
        fingerprints_path=tmp_path / "fingerprints.csv",
        **overrides,
    )
    return create_app(store=store, settings=config, start_ingest=False)


def test_the_server_installs_the_households_appliances(store, tmp_path):
    with TestClient(_app(store, tmp_path, HOUSEHOLD)) as client:
        roster = {row["id"]: row for row in client.get("/api/appliances").json()}
        rules_state = client.get("/api/rules").json()
        wizard_state = client.get("/api/wizard").json()

    # Exactly the file's appliances: none of the example household's.
    assert sorted(roster) == ["fridge", "rice_cooker", "tv"]
    assert roster["fridge"]["protected"] == 1
    assert roster["rice_cooker"]["heating"] == 1
    assert roster["rice_cooker"]["plug_device"] == "plug_rice"
    assert roster["tv"]["plug_device"] is None
    assert rules_state["left_on_targets"] == ["rice_cooker"]
    assert wizard_state["classes"] == ["rice_cooker", "tv"]


def test_the_rules_watch_the_households_standby_targets(store, tmp_path):
    with TestClient(_app(store, tmp_path, HOUSEHOLD)) as client:
        engine = client.app.state.rules
        assert engine.config_targets("standby_draw") == ("tv",)


def test_an_appliance_removed_from_the_file_leaves_the_registry(store, tmp_path):
    """A restart after deleting a block must not leave the old row behind,
    still switchable under the flags it used to have."""
    with TestClient(_app(store, tmp_path, HOUSEHOLD)):
        pass
    fewer = HOUSEHOLD.split("[[appliance]]\nid = \"fridge\"")[0]
    with TestClient(_app(store, tmp_path, fewer)) as client:
        ids = [row["id"] for row in client.get("/api/appliances").json()]
    assert ids == ["rice_cooker"]


def test_trained_classes_from_the_environment_override_the_file(store, tmp_path):
    app = _app(store, tmp_path, HOUSEHOLD, trained_classes=("fridge",))
    with TestClient(app) as client:
        assert client.get("/api/wizard").json()["classes"] == ["fridge"]


def test_a_broken_file_stops_the_server_starting(store, tmp_path):
    """Protected and heating are the gate's inputs. A server that started
    anyway would be switching relays from a registry nobody wrote."""
    broken = HOUSEHOLD.replace("protected = true", 'protected = "yes"')
    with pytest.raises(ApplianceFileError, match="fridge"):
        with TestClient(_app(store, tmp_path, broken)):
            pass
    assert store.appliances() == []
