import pytest
from jsonschema import ValidationError

from conftest import load_example


@pytest.mark.parametrize(
    "name",
    [
        "telemetry-single-load",
        "telemetry-negative-residual",
        "telemetry-simulator-null-health",
        "telemetry-unknown-load",
    ],
)
def test_examples_validate(telemetry_validator, name):
    telemetry_validator.validate(load_example(name))


def test_negative_residual_is_accepted(telemetry_validator):
    """Non-negotiable #2: the residual may go negative and is reported anyway."""
    payload = load_example("telemetry-single-load")
    payload["attribution"]["residual_w"] = -37.5
    telemetry_validator.validate(payload)


def test_simulator_must_have_null_health(telemetry_validator):
    """Sampler health counters are meaningless off-target."""
    payload = load_example("telemetry-simulator-null-health")
    payload["health"] = {"isr_overruns": 0, "worst_isr_us": 41, "cycles_dropped": 0}
    with pytest.raises(ValidationError):
        telemetry_validator.validate(payload)


def test_device_must_have_health(telemetry_validator):
    payload = load_example("telemetry-single-load")
    assert payload["source"] == "device"
    payload["health"] = None
    with pytest.raises(ValidationError):
        telemetry_validator.validate(payload)


def test_unknown_load_id_accepted(telemetry_validator):
    payload = load_example("telemetry-single-load")
    payload["attribution"]["active"] = [
        {"id": "unknown_3", "w": 112.0, "since": 1754035188.0}
    ]
    telemetry_validator.validate(payload)


def test_residual_id_rejected_in_active(telemetry_validator):
    """__residual__ is the residual_w field and an S3 ledger row, never an active appliance."""
    payload = load_example("telemetry-single-load")
    payload["attribution"]["active"] = [
        {"id": "__residual__", "w": 10.0, "since": 1754035188.0}
    ]
    with pytest.raises(ValidationError):
        telemetry_validator.validate(payload)


def test_unknown_top_level_field_rejected(telemetry_validator):
    payload = load_example("telemetry-single-load")
    payload["surprise"] = 1
    with pytest.raises(ValidationError):
        telemetry_validator.validate(payload)
