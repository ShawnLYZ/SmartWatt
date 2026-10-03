import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from smartwatt_sim.appliances import DEFAULT_APPLIANCES_PATH, load_appliances
from smartwatt_sim.payload import SimState, build_event, build_telemetry

SCHEMAS = Path(__file__).resolve().parents[2] / "contract" / "schemas"
V, F = 240.0, 50.0


def _validator(stem: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / f"{stem}.schema.json").read_text()))


@pytest.fixture(scope="module")
def appliances():
    return load_appliances(DEFAULT_APPLIANCES_PATH)


@pytest.fixture
def state():
    return SimState()


def test_telemetry_validates(appliances, state):
    payload = build_telemetry(
        1754035200.0, V, F, [appliances["kettle"]], state, floor_w=6.0
    )
    _validator("telemetry").validate(payload)


def test_telemetry_source_is_simulator(appliances, state):
    payload = build_telemetry(1754035200.0, V, F, [], state, floor_w=6.0)
    assert payload["source"] == "simulator"


def test_simulator_health_is_null(appliances, state):
    payload = build_telemetry(1754035200.0, V, F, [], state, floor_w=6.0)
    assert payload["health"] is None


def test_seq_increments(appliances, state):
    first = build_telemetry(1.0, V, F, [], state, floor_w=6.0)
    second = build_telemetry(2.0, V, F, [], state, floor_w=6.0)
    assert second["seq"] == first["seq"] + 1


def test_energy_accumulates(appliances, state):
    build_telemetry(1.0, V, F, [appliances["kettle"]], state, floor_w=6.0)
    second = build_telemetry(2.0, V, F, [appliances["kettle"]], state, floor_w=6.0)
    assert second["energy"]["wh_session"] > 0.0


def test_active_watts_sum_close_to_total(appliances, state):
    active = [appliances["kettle"], appliances["desk_fan"]]
    payload = build_telemetry(1.0, V, F, active, state, floor_w=6.0)
    attributed = sum(item["w"] for item in payload["attribution"]["active"])
    total = payload["electrical"]["p"]
    assert payload["attribution"]["residual_w"] == pytest.approx(total - attributed)
    # P superposes, so with no noise the parts really do sum to the whole
    assert payload["attribution"]["residual_w"] == pytest.approx(0.0, abs=1e-9)


def test_noise_can_drive_residual_negative(appliances, state):
    """The dashboard must be exercised against the honest case."""
    seen_negative = False
    for n in range(400):
        payload = build_telemetry(
            float(n), V, F, [appliances["desk_fan"]], state,
            floor_w=6.0, noise_w=4.0,
        )
        if payload["attribution"]["residual_w"] < 0:
            seen_negative = True
            break
    assert seen_negative


def test_noisy_payload_stays_physically_consistent(appliances, state):
    """|P| <= S always: noise enters as current, so every quantity moves together."""
    for n in range(200):
        payload = build_telemetry(
            float(n), V, F, [appliances["desk_fan"]], state,
            floor_w=6.0, noise_w=4.0,
        )
        e = payload["electrical"]
        assert abs(e["p"]) <= e["s"] + 1e-9
        assert e["pf_true"] == pytest.approx(e["p"] / e["s"])


def test_negative_residual_still_validates(appliances, state):
    payload = build_telemetry(1.0, V, F, [appliances["desk_fan"]], state, floor_w=6.0)
    payload["attribution"]["residual_w"] = -12.5
    _validator("telemetry").validate(payload)


def test_unknown_id_in_active(appliances, state):
    payload = build_telemetry(
        1.0, V, F, [appliances["clothes_iron"]], state,
        floor_w=6.0, unknown_ids={"clothes_iron": "unknown_1"},
    )
    assert payload["attribution"]["active"][0]["id"] == "unknown_1"
    _validator("telemetry").validate(payload)


def test_event_validates(appliances, state):
    kettle = appliances["kettle"]
    payload = build_event(
        1754035188.412, "on", kettle, before=[], after=[kettle],
        state=state, v_rms=V, trained_labels={"kettle"},
    )
    _validator("event").validate(payload)


def test_event_features_are_derived_not_invented(appliances, state):
    from smartwatt_sim.features import derive_features

    kettle = appliances["kettle"]
    payload = build_event(
        1.0, "on", kettle, before=[], after=[kettle],
        state=state, v_rms=V, trained_labels={"kettle"},
    )
    expected = derive_features(V, before=[], after=[kettle], switched=kettle)
    for name, value in expected.items():
        assert payload["features"][name] == pytest.approx(value), name


def test_untrained_load_is_rejected(appliances, state):
    iron = appliances["clothes_iron"]
    payload = build_event(
        1.0, "on", iron, before=[], after=[iron],
        state=state, v_rms=V, trained_labels={"kettle"},
        rejected=True, reason="distance_threshold", attributed_to="unknown_1",
    )
    assert payload["rejected"] is True
    assert payload["label"] is None
    _validator("event").validate(payload)


def test_ambiguous_event_attributes_to_nothing(appliances, state):
    fan = appliances["desk_fan"]
    payload = build_event(
        1.0, "on", fan, before=[], after=[fan],
        state=state, v_rms=V, trained_labels={"desk_fan"},
        ambiguous=True, reason="overlapping_edges", attributed_to=None,
    )
    assert payload["ambiguous"] is True
    assert payload["attributed_to"] is None
    _validator("event").validate(payload)


def test_event_has_three_neighbours(appliances, state):
    kettle = appliances["kettle"]
    payload = build_event(
        1.0, "on", kettle, before=[], after=[kettle],
        state=state, v_rms=V, trained_labels={"kettle"},
    )
    assert len(payload["neighbours"]) == 3
