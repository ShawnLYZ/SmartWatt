"""Validate firmware-emitted JSON against the contract schemas.

MECHANISM NOTE. The S5 spec flags this as a deviation from PRD.md's literal
wording ("firmware native tests validate emitted JSON against the same
schema"). The guarantee is identical and arguably stronger: the emitted JSON
is checked by the EXACT validator the server runs, rather than by a second
C++ implementation of schema validation that could itself diverge. It also
keeps a JSON-schema dependency out of the firmware entirely.

The native Unity suite writes payloads to firmware/test/output/; this reads
them back. The build fails if either half is missing, or stale.

WHAT THE SCHEMAS CANNOT CATCH, AND THIS FILE THEREFORE MUST. A schema
constrains shapes and ranges, not values. Every field below is emitted from a
hand-written `w.append` argument list, so a transposition -- `p` and `q1`
swapped, `wh_session` where `floor_w` belongs, `ts` degraded to `%.0f` --
produces a payload that is still perfectly schema-valid. The expected
payloads here pin the actual numbers against the fixtures the emitter was
handed, which is the only thing that turns such a swap red.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

FIRMWARE = Path(__file__).resolve().parents[1]
OUTPUT = FIRMWARE / "test" / "output"
SCHEMAS = FIRMWARE.parent / "contract" / "schemas"

RERUN = "Run: cd firmware && pio test -e native -f test_emit"

#: Sources whose changes invalidate every emitted payload.
#:
#: NOT just the emitter. The payloads are built by running real library code:
#: `features` supplies every value under `features`, `track` supplies
#: `attribution.active` and the residual, `detector.h` supplies the
#: DetectedEvent defaults the fixtures lean on (inrush_ratio's 1.0 among them)
#: and `knn.h` supplies Classification, including the neighbour count the
#: emitter now reads. Editing extract() and running only `uv run pytest` used
#: to give a green bar over payloads emitted by the PREVIOUS extract(), and
#: with no .github/workflows in the repo this test is the only thing enforcing
#: native-before-bridge ordering.
SOURCES = (
    FIRMWARE / "lib" / "emit" / "payload.h",
    FIRMWARE / "lib" / "emit" / "payload.cpp",
    FIRMWARE / "lib" / "features" / "features.cpp",
    FIRMWARE / "lib" / "track" / "tracker.cpp",
    FIRMWARE / "lib" / "events" / "detector.h",
    FIRMWARE / "lib" / "classify" / "knn.h",
    FIRMWARE / "test" / "test_emit" / "test_emit.cpp",
)

EXPECTED_OUTPUTS = {
    "telemetry-device": "telemetry",
    "telemetry-simulated": "telemetry",
    "telemetry-negative-residual": "telemetry",
    "event-on-confident": "event",
    "event-rejected-unknown": "event",
    "event-ambiguous-overlap": "event",
    "event-off-reassigned": "event",
    "event-below-floor": "event",
}

# --------------------------------------------------------------------------
# The fixtures the native suite hands the emitter, mirrored here.
# --------------------------------------------------------------------------

#: test_emit.cpp:metrics()
ELECTRICAL = {
    "vrms": 239.4,
    "irms": 3.812,
    "p": 902.5,
    "q1": 118.3,
    "dist": 41.2,
    "s": 912.6,
    "pf_true": 0.989,
    "pf_disp": 0.997,
    "freq": 49.98,
    "range": "high",
}
#: test_emit.cpp:device_context()
SEQ = 12345
FINGERPRINT_ID = "a3f19c2e"
FLOOR_W = 8.5
ENERGY = {"wh_session": 4231.7, "wh_today": 1204.3}
#: test_emit.cpp:health() -- deliberately three distinct values, so a
#: transposition of any two of them is visible.
HEALTH = {"isr_overruns": 0, "worst_isr_us": 41, "cycles_dropped": 2}

KETTLE_W, UNKNOWN_W = 848.0, 41.5
FAN_W, FAN_TOTAL = 46.2, 44.9


def _telemetry(ts, source, fingerprint_id, electrical, active, residual, health):
    return {
        "schema": "smartwatt.telemetry.v1",
        "ts": ts,
        "source": source,
        "seq": SEQ,
        "fingerprint_id": fingerprint_id,
        "electrical": electrical,
        "attribution": {
            "active": active,
            "residual_w": residual,
            "floor_w": FLOOR_W,
        },
        "energy": ENERGY,
        "health": health,
    }


def _sparse_features(delta_p, settle_cycles=0):
    """extract() on a cycle pair carrying nothing but `p`.

    The zeros are not filler: they are what extract() returns when `before`
    and `after` differ in no field but `p`, and they pin the degenerate
    branches. delta_s is zero, so pf_true takes extract()'s `1.0` fallback
    rather than dividing; the fundamental VA is |delta_p|, so pf_disp is
    exactly 1.0; and inrush_ratio is DetectedEvent's own default of 1.0,
    not zero.
    """
    return {
        "delta_p": delta_p,
        "delta_q1": 0.0,
        "delta_dist": 0.0,
        "delta_s": 0.0,
        "pf_disp": 1.0,
        "pf_true": 1.0,
        "delta_irms": 0.0,
        "delta_crest": 0.0,
        "h3_h1": 0.0,
        "h5_h1": 0.0,
        "h7_h1": 0.0,
        "inrush_ratio": 1.0,
        "settle_cycles": settle_cycles,
        "log_delta_p": math.log1p(delta_p),
    }


#: The distance emit_event reports for a neighbour slot the classifier never
#: filled. It used to be the Neighbour default of 0.0, which reads as a
#: PERFECT match in the array US16 presents as the explanation of the
#: classification; padding has to sort after every real neighbour instead.
UNFILLED_DISTANCE = 1e9


def _placeholder_neighbours():
    """What emit_event writes for the slots Knn::classify never filled.

    The schema requires exactly three neighbours, so a short fingerprint table
    still emits three. `label` and `training_id` of "none" are what identify a
    slot as padding, and the distance must not contradict them.
    """
    return [
        {"label": "none", "distance": UNFILLED_DISTANCE, "training_id": "none"}
        for _ in range(3)
    ]


# event-on-confident's cycle pair: a standing load, then that load plus a
# kettle. Both cycles are rebuilt here from p/q1/dist alone, so delta_s,
# delta_irms, pf_disp and pf_true below are genuinely recomputed rather than
# read back. See test_emitted_features_match_the_extractor for exactly what
# that does and does not prove.
VRMS = 239.4
_BEFORE = {"p": 12.0, "q1": 4.0, "dist": 2.5, "crest": 1.90,
           "h3": 0.14, "h5": 0.08, "h7": 0.05}
_AFTER = {"p": 860.2, "q1": 35.5, "dist": 20.9, "crest": 1.423,
          "h3": 0.021, "h5": 0.009, "h7": 0.004}
for _c in (_BEFORE, _AFTER):
    _c["s"] = math.sqrt(_c["p"] ** 2 + _c["q1"] ** 2 + _c["dist"] ** 2)
    _c["irms"] = _c["s"] / VRMS

#: The settled step the detector judged, carried on the event: 860.2 - 12.0.
STEP_W = 848.2
_DELTA_Q1 = _AFTER["q1"] - _BEFORE["q1"]
_DELTA_S = _AFTER["s"] - _BEFORE["s"]

CONFIDENT_FEATURES = {
    "delta_p": STEP_W,
    "delta_q1": _DELTA_Q1,
    "delta_dist": _AFTER["dist"] - _BEFORE["dist"],
    "delta_s": _DELTA_S,
    "pf_disp": STEP_W / math.hypot(STEP_W, _DELTA_Q1),
    "pf_true": STEP_W / _DELTA_S,
    "delta_irms": _AFTER["irms"] - _BEFORE["irms"],
    "delta_crest": abs(_AFTER["crest"] - _BEFORE["crest"]),
    # The BUSIER state's harmonics. before carries 0.14/0.08/0.05, so
    # picking the wrong side is visible here.
    "h3_h1": _AFTER["h3"],
    "h5_h1": _AFTER["h5"],
    "h7_h1": _AFTER["h7"],
    "inrush_ratio": 1.02,
    "settle_cycles": 3,
    "log_delta_p": math.log1p(STEP_W),
}

EXPECTED_PAYLOADS = {
    "telemetry-device": _telemetry(
        ts=1754035200.0,
        source="device",
        fingerprint_id=FINGERPRINT_ID,
        electrical=ELECTRICAL,
        active=[
            {"id": "kettle", "w": KETTLE_W, "since": 1754035188.0},
            {"id": "unknown_1", "w": UNKNOWN_W, "since": 1754035190.0},
        ],
        # 902.5 measured minus the two attributed loads.
        residual=ELECTRICAL["p"] - KETTLE_W - UNKNOWN_W,
        health=HEALTH,
    ),
    "telemetry-simulated": _telemetry(
        ts=1754035200.0,
        source="simulator",
        fingerprint_id=None,
        electrical=ELECTRICAL,
        active=[],
        # Nothing attributed and observe_total never called: Tracker's
        # initial residual_, not the measured total.
        residual=0.0,
        health=None,
    ),
    "telemetry-negative-residual": _telemetry(
        ts=1754035260.0,
        source="device",
        fingerprint_id=FINGERPRINT_ID,
        electrical=dict(ELECTRICAL, p=FAN_TOTAL),
        active=[{"id": "desk_fan", "w": FAN_W, "since": 100.0}],
        residual=FAN_TOTAL - FAN_W,
        health=HEALTH,
    ),
    "event-on-confident": {
        "schema": "smartwatt.event.v1",
        "ts": 1754035188.412,
        "source": "device",
        "seq": SEQ,
        "edge": "on",
        "label": "kettle",
        "confidence": 0.94,
        "rejected": False,
        "ambiguous": False,
        "reason": None,
        "attributed_to": "kettle",
        "features": CONFIDENT_FEATURES,
        "neighbours": [
            {
                "label": "kettle",
                "distance": 0.31 + 0.13 * n,
                "training_id": f"kettle-on-{n + 1:03d}",
            }
            for n in range(3)
        ],
    },
    "event-rejected-unknown": {
        "schema": "smartwatt.event.v1",
        "ts": 1.0,
        "source": "device",
        "seq": SEQ,
        "edge": "on",
        "label": None,
        "confidence": 0.0,
        "rejected": True,
        "ambiguous": False,
        "reason": "distance_threshold",
        "attributed_to": "unknown_1",
        "features": _sparse_features(305.0),
        "neighbours": _placeholder_neighbours(),
    },
    "event-ambiguous-overlap": {
        "schema": "smartwatt.event.v1",
        "ts": 1.0,
        "source": "device",
        "seq": SEQ,
        "edge": "on",
        "label": None,
        "confidence": 0.0,
        "rejected": False,
        "ambiguous": True,
        "reason": "overlapping_edges",
        "attributed_to": None,
        "features": _sparse_features(89.4),
        "neighbours": _placeholder_neighbours(),
    },
    # The tracker rejected this one on the DETECTION FLOOR, which the
    # classifier never sees -- so the classification is a confident "kettle" at
    # 0.94 while the attribution carries no label at all. The emitter reads
    # `label` from the attribution and `confidence` from the classification,
    # and shipped `{"label": null, "confidence": 0.9400}`: 94% certainty in a
    # name that is not there. Schema-valid, so only a pinned value catches it.
    "event-below-floor": {
        "schema": "smartwatt.event.v1",
        "ts": 1.0,
        "source": "device",
        "seq": SEQ,
        "edge": "on",
        "label": None,
        "confidence": 0.0,
        "rejected": True,
        "ambiguous": False,
        "reason": "below_floor",
        "attributed_to": None,
        "features": _sparse_features(3.0, settle_cycles=3),
        # The classifier's own neighbours survive: refusing to name the event
        # is not the same as refusing to explain it.
        "neighbours": [
            {
                "label": "kettle",
                "distance": 0.31 + 0.13 * n,
                "training_id": f"kettle-on-{n + 1:03d}",
            }
            for n in range(3)
        ],
    },
    "event-off-reassigned": {
        "schema": "smartwatt.event.v1",
        "ts": 1.0,
        "source": "device",
        "seq": SEQ,
        "edge": "off",
        # The classifier's answer and the tracker's, both preserved.
        "label": "desk_fan",
        "confidence": 0.61,
        "rejected": False,
        "ambiguous": False,
        "reason": None,
        "attributed_to": "incandescent_lamp",
        # A signed step of -40.0 W; the magnitude is the feature and the
        # sign is carried by `edge`.
        "features": _sparse_features(40.0),
        "neighbours": [
            {
                "label": "incandescent_lamp",
                "distance": 0.7 + 0.1 * n,
                "training_id": f"lamp-off-{n + 1:03d}",
            }
            for n in range(3)
        ],
    },
}

#: The emitter prints six decimals, so half an ulp of the printed
#: representation is 5e-7. The C++ fixture's `s` and `irms` literals are
#: transcribed to ten decimals, adding ~1e-10. 1e-6 covers both and is still
#: three orders of magnitude tighter than any real defect.
TOLERANCE = 1e-6


def _validator(stem: str) -> Draft202012Validator:
    return Draft202012Validator(
        json.loads((SCHEMAS / f"{stem}.schema.json").read_text())
    )


def _payload(name: str) -> dict:
    path = OUTPUT / f"{name}.json"
    if not path.exists():
        pytest.skip("run the native emit suite first")
    return json.loads(path.read_text())


def _assert_same(actual, expected, where: str) -> None:
    """Compare a parsed payload against its expected value, field by field.

    Integers are compared by TYPE as well as value. JSON Schema 2020-12
    counts 3.0 as an integer, so `"type": "integer"` in the contract cannot
    tell `3` from `3.0`; json.loads can, and this is where that distinction
    is enforced for seq, the health counters and settle_cycles alike.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), where
        assert set(actual) == set(expected), (
            f"{where}: keys differ, "
            f"extra={sorted(set(actual) - set(expected))} "
            f"missing={sorted(set(expected) - set(actual))}"
        )
        for key in expected:
            _assert_same(actual[key], expected[key], f"{where}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), where
        assert len(actual) == len(expected), (
            f"{where}: {len(actual)} items, expected {len(expected)}"
        )
        for n, (got, want) in enumerate(zip(actual, expected)):
            _assert_same(got, want, f"{where}[{n}]")
    elif isinstance(expected, bool) or expected is None:
        assert actual is expected, f"{where}: {actual!r} != {expected!r}"
    elif isinstance(expected, int):
        assert type(actual) is int, (
            f"{where}: {actual!r} is {type(actual).__name__}, "
            "expected a JSON integer"
        )
        assert actual == expected, f"{where}: {actual!r} != {expected!r}"
    elif isinstance(expected, float):
        assert isinstance(actual, float), f"{where}: {actual!r} is not a number"
        assert actual == pytest.approx(expected, abs=TOLERANCE), (
            f"{where}: {actual!r} != {expected!r}"
        )
    else:
        assert actual == expected, f"{where}: {actual!r} != {expected!r}"


def test_the_native_suite_was_run_first():
    """Run `pio test -e native -f test_emit` before this.

    Every per-payload test skips when its file is absent, so without this one
    an unrun native half would report success while validating nothing. This
    one asserts, so it goes red instead. Path.glob on a missing directory
    returns [] rather than raising, so it fires there too.
    """
    produced = {p.stem for p in OUTPUT.glob("*.json")}
    missing = set(EXPECTED_OUTPUTS) - produced
    assert not missing, f"missing emitted payloads: {sorted(missing)}. {RERUN}"
    # Drift the other way: a payload the native half saves that nobody
    # registered here would otherwise sit in the directory unvalidated.
    unregistered = produced - set(EXPECTED_OUTPUTS)
    assert not unregistered, (
        f"emitted payloads nothing validates: {sorted(unregistered)}. "
        "Add them to EXPECTED_OUTPUTS and EXPECTED_PAYLOADS."
    )


def test_the_emitted_payloads_are_not_stale():
    """Existence is not freshness.

    The payloads are gitignored build products. Someone who edits payload.cpp
    and runs only `uv run pytest` would otherwise validate the PREVIOUS run's
    files and get a green bar; so would a native run that failed before its
    save() and left yesterday's file in place. Both are the same "green while
    validating nothing" failure the guard above closes from the other side.
    """
    produced = sorted(OUTPUT.glob("*.json"))
    assert produced, f"no emitted payloads at all. {RERUN}"
    oldest = min(p.stat().st_mtime for p in produced)
    changed_since = sorted(s.name for s in SOURCES if s.stat().st_mtime > oldest)
    assert not changed_since, (
        f"{changed_since} changed after the payloads were written, so this "
        f"would validate the previous build. {RERUN}"
    )


@pytest.mark.parametrize("name,kind", sorted(EXPECTED_OUTPUTS.items()))
def test_emitted_payload_validates(name, kind):
    _validator(kind).validate(_payload(name))


@pytest.mark.parametrize("name", sorted(EXPECTED_PAYLOADS))
def test_emitted_payload_carries_the_expected_values(name):
    """Every field of every payload, against the fixture it came from.

    This is what a schema cannot do. Swap `p` and `q1` in the emitter, or
    feed `floor_w` from `wh_session`, or drop `ts` to `%.0f`, and the payload
    stays schema-valid and every structural test stays green -- only these
    comparisons go red.
    """
    _assert_same(_payload(name), EXPECTED_PAYLOADS[name], name)


def test_negative_residual_survives_to_the_wire():
    payload = _payload("telemetry-negative-residual")
    assert payload["attribution"]["residual_w"] < 0
    # 44.9 W measured against 46.2 W attributed to desk_fan.
    assert payload["attribution"]["residual_w"] == pytest.approx(
        FAN_TOTAL - FAN_W, abs=TOLERANCE
    )
    _validator("telemetry").validate(payload)


def test_simulated_health_is_null():
    payload = _payload("telemetry-simulated")
    assert payload["source"] != "device"
    assert payload["health"] is None


def test_a_null_label_never_ships_a_confidence():
    """A confidence is confidence IN A NAME, so no name means no number.

    Both rejection sources, from opposite directions. event-rejected-unknown is
    the classifier's own, where knn.cpp zeroes label and confidence together.
    event-below-floor is the tracker's: the classifier never sees the detection
    floor, so it stayed confident at 0.94 and only the attribution refused --
    and the emitter reads the two fields from those two different structs.
    """
    for name in ("event-rejected-unknown", "event-below-floor",
                 "event-ambiguous-overlap"):
        payload = _payload(name)
        assert payload["label"] is None, name
        assert payload["confidence"] == 0.0, name
        _validator("event").validate(payload)

    # The converse, so this cannot be satisfied by zeroing every confidence.
    named = _payload("event-on-confident")
    assert named["label"] == "kettle"
    assert named["confidence"] == pytest.approx(0.94, abs=TOLERANCE)


def test_unfilled_neighbour_slots_do_not_read_as_perfect_matches():
    """Rank 3 of a two-row table is not the best evidence there is.

    The schema requires exactly three neighbours, so a short fingerprint table
    still emits three -- and an unfilled slot used to render as
    {"label": "unknown", "distance": 0.0, "training_id": "none"}, a
    zero-distance match to an appliance called "unknown", inside the array
    US16 offers as the explanation of the classification.
    """
    for name in ("event-rejected-unknown", "event-ambiguous-overlap"):
        neighbours = _payload(name)["neighbours"]
        assert len(neighbours) == 3, name
        for n, entry in enumerate(neighbours):
            assert entry["label"] == "none", f"{name}[{n}]"
            assert entry["training_id"] == "none", f"{name}[{n}]"
            assert entry["distance"] == UNFILLED_DISTANCE, f"{name}[{n}]"

    # Real neighbours are untouched by any of that, zero distances included:
    # event-below-floor's nearest is a genuine 0.31 from a named training row.
    real = _payload("event-below-floor")["neighbours"]
    assert [e["label"] for e in real] == ["kettle"] * 3
    assert max(e["distance"] for e in real) < UNFILLED_DISTANCE


def test_reassigned_event_keeps_both_names():
    payload = _payload("event-off-reassigned")
    assert payload["label"] != payload["attributed_to"]
    _validator("event").validate(payload)


def test_features_are_exactly_the_fourteen():
    payload = _payload("event-on-confident")
    assert len(payload["features"]) == 14
    assert "edge" not in payload["features"]


def test_settle_cycles_is_a_json_integer_not_a_float():
    """The one contract rule schema validation cannot enforce.

    JSON Schema 2020-12 counts a float with a zero fraction as an integer, so
    `3.0` satisfies `"type": "integer"` and the schema would accept
    settle_cycles emitted through the %.6f path. Only the raw token tells the
    two apart: json.loads gives `int` for `3` and `float` for `3.0`.
    """
    for name, kind in EXPECTED_OUTPUTS.items():
        if kind != "event":
            continue
        settle = _payload(name)["features"]["settle_cycles"]
        assert type(settle) is int, f"{name}: settle_cycles is {type(settle)}"


def test_emitted_features_match_the_extractor():
    """The fourteen values of event-on-confident, recomputed here.

    WHAT THIS PROVES. delta_s, delta_irms, pf_disp and pf_true are rebuilt
    from the cycle pair's p/q1/dist alone, and the fixture is chosen so each
    differs from the cycle field it might have been copied from instead:
    pf_true is 0.999891 against after.pf_true's 0.998855, pf_disp 0.999311
    against 0.999150, delta_s 848.292071 against after.s's 861.185868, and
    h3_h1 takes the busier state's 0.021 rather than before's 0.140. A
    version of extract() that copied any of those through would go red here.

    WHAT IT DOES NOT PROVE. The C++ fixture's `s` and `irms` are typed-in
    literals accurate to ten decimals, not computed in C++, so this does not
    show that the fixture's own cycles are self-consistent to full double
    precision -- only to ~1e-10, which the six decimals on the wire could not
    resolve in any case.
    """
    features = _payload("event-on-confident")["features"]
    _assert_same(features, CONFIDENT_FEATURES, "event-on-confident.features")
