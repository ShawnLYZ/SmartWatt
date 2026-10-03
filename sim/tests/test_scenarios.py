import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from smartwatt_sim.scenarios import SCENARIOS, run

SCHEMAS = Path(__file__).resolve().parents[2] / "contract" / "schemas"

EXPECTED = {
    "baseline", "single", "overlap", "simultaneous",
    "unknown", "below-floor", "cliff", "demo",
}


@pytest.fixture(scope="module")
def validators():
    return {
        stem: Draft202012Validator(
            json.loads((SCHEMAS / f"{stem}.schema.json").read_text())
        )
        for stem in ("telemetry", "event")
    }


def test_eight_scenarios():
    assert set(SCENARIOS) == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_payload_validates(validators, name):
    """A simulator that emits invalid payloads is worse than no simulator."""
    count = 0
    for kind, payload in run(name):
        validators[kind].validate(payload)
        count += 1
    assert count > 0


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_seq_is_strictly_monotonic(name):
    seqs = [payload["seq"] for _, payload in run(name)]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_timestamps_are_non_decreasing(name):
    stamps = [payload["ts"] for _, payload in run(name)]
    assert stamps == sorted(stamps)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_electrical_quantities_are_physically_consistent(name):
    """No payload may claim more real power than apparent power."""
    for kind, payload in run(name):
        if kind != "telemetry":
            continue
        e = payload["electrical"]
        assert abs(e["p"]) <= e["s"] + 1e-9


def test_baseline_emits_no_events():
    kinds = {kind for kind, _ in run("baseline")}
    assert kinds == {"telemetry"}


def test_single_emits_one_on_and_one_off():
    events = [p for kind, p in run("single") if kind == "event"]
    assert [e["edge"] for e in events] == ["on", "off"]
    assert all(e["label"] is not None for e in events)


def test_overlap_switches_while_another_runs():
    active_counts = [
        len(p["attribution"]["active"])
        for kind, p in run("overlap")
        if kind == "telemetry"
    ]
    assert max(active_counts) >= 2


def test_simultaneous_flags_ambiguous():
    events = [p for kind, p in run("simultaneous") if kind == "event"]
    ambiguous = [e for e in events if e["ambiguous"]]
    assert ambiguous
    assert all(e["reason"] == "overlapping_edges" for e in ambiguous)
    assert all(e["attributed_to"] is None for e in ambiguous)


def test_unknown_rejects_and_still_counts_the_energy():
    """US14: refusing to name something never means losing track of it."""
    events = [p for kind, p in run("unknown") if kind == "event"]
    rejected = [e for e in events if e["rejected"]]
    assert rejected
    assert all(e["label"] is None for e in rejected)
    assert any(e["attributed_to"] is not None for e in rejected)

    ids = {
        item["id"]
        for kind, p in run("unknown")
        if kind == "telemetry"
        for item in p["attribution"]["active"]
    }
    assert any(i.startswith("unknown_") for i in ids)


def test_below_floor_reason():
    events = [p for kind, p in run("below-floor") if kind == "event"]
    assert any(e["reason"] == "below_floor" for e in events)


def test_cliff_crosses_400_kwh():
    """S4's Cliff Gauge cannot be built without this. Waiting a real month
    to see the gauge in its interesting state is not an option."""
    telemetry = [p for kind, p in run("cliff") if kind == "telemetry"]
    kwh = telemetry[-1]["energy"]["wh_today"] / 1000.0
    assert kwh > 400.0
    assert telemetry[0]["energy"]["wh_today"] / 1000.0 < 400.0


def test_demo_covers_every_trained_class():
    labels = {
        p["label"]
        for kind, p in run("demo")
        if kind == "event" and p["label"]
    }
    assert labels >= {"kettle", "desk_fan", "incandescent_lamp",
                      "led_bulb", "laptop_charger"}


def test_unknown_scenario_raises():
    with pytest.raises(KeyError):
        list(run("nonexistent"))
