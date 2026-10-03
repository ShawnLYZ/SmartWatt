import pytest
from jsonschema import Draft202012Validator, ValidationError

from conftest import FEATURE_NAMES, load_example, load_schema


@pytest.fixture
def command_validator():
    return Draft202012Validator(load_schema("command"))


@pytest.fixture
def fingerprints_validator():
    return Draft202012Validator(load_schema("fingerprints"))


def test_command_example_validates(command_validator):
    command_validator.validate(load_example("command-off"))


@pytest.mark.parametrize("payload", ["ON", "OFF"])
def test_tasmota_payloads(command_validator, payload):
    command_validator.validate({"device": "plug_kettle", "command": payload})


def test_lowercase_command_rejected(command_validator):
    """Tasmota convention is uppercase; accepting both invites drift."""
    with pytest.raises(ValidationError):
        command_validator.validate({"device": "plug_kettle", "command": "off"})


def test_toggle_rejected(command_validator):
    """TOGGLE would make the safety gate's OFF check meaningless."""
    with pytest.raises(ValidationError):
        command_validator.validate({"device": "plug_kettle", "command": "TOGGLE"})


def test_fingerprints_example_validates(fingerprints_validator):
    fingerprints_validator.validate(load_example("fingerprints-minimal"))


def test_fingerprint_row_needs_all_fourteen(fingerprints_validator):
    payload = load_example("fingerprints-minimal")
    del payload["rows"][0]["features"]["h7_h1"]
    with pytest.raises(ValidationError):
        fingerprints_validator.validate(payload)


def test_normalisation_covers_every_feature():
    payload = load_example("fingerprints-minimal")
    assert set(payload["normalisation"]["mean"]) == set(FEATURE_NAMES)
    assert set(payload["normalisation"]["std"]) == set(FEATURE_NAMES)


def test_threshold_required(fingerprints_validator):
    payload = load_example("fingerprints-minimal")
    del payload["rejection_threshold"]
    with pytest.raises(ValidationError):
        fingerprints_validator.validate(payload)
