import pytest
from jsonschema import ValidationError

from conftest import FEATURE_NAMES, load_example


@pytest.mark.parametrize(
    "name",
    [
        "event-on-confident",
        "event-off-reassigned",
        "event-rejected-unknown",
        "event-ambiguous-overlap",
        "event-below-floor",
    ],
)
def test_examples_validate(event_validator, name):
    event_validator.validate(load_example(name))


def test_exactly_fourteen_features():
    payload = load_example("event-on-confident")
    assert set(payload["features"]) == set(FEATURE_NAMES)
    assert len(FEATURE_NAMES) == 14


def test_fifteenth_feature_rejected(event_validator):
    """A feature appearing by accident is a schema failure, not a silent extension."""
    payload = load_example("event-on-confident")
    payload["features"]["post_event_variance"] = 0.3
    with pytest.raises(ValidationError):
        event_validator.validate(payload)


def test_missing_feature_rejected(event_validator):
    payload = load_example("event-on-confident")
    del payload["features"]["h7_h1"]
    with pytest.raises(ValidationError):
        event_validator.validate(payload)


def test_edge_is_outside_features():
    """The classifier must be structurally incapable of seeing edge direction."""
    payload = load_example("event-on-confident")
    assert "edge" in payload
    assert "edge" not in payload["features"]


def test_label_and_attributed_to_may_differ(event_validator):
    """US17: the tracker's state-consistency filter must leave evidence it fired."""
    payload = load_example("event-off-reassigned")
    assert payload["label"] != payload["attributed_to"]
    event_validator.validate(payload)


def test_neighbours_exactly_three(event_validator):
    payload = load_example("event-on-confident")
    assert len(payload["neighbours"]) == 3
    payload["neighbours"] = payload["neighbours"][:2]
    with pytest.raises(ValidationError):
        event_validator.validate(payload)


@pytest.mark.parametrize(
    "reason",
    ["below_floor", "no_settle", "overlapping_edges", "distance_threshold"],
)
def test_all_reasons_accepted(event_validator, reason):
    payload = load_example("event-rejected-unknown")
    payload["reason"] = reason
    event_validator.validate(payload)


def test_invented_reason_rejected(event_validator):
    payload = load_example("event-rejected-unknown")
    payload["reason"] = "vibes"
    with pytest.raises(ValidationError):
        event_validator.validate(payload)
