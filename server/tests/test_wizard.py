import json

import pytest
from jsonschema import Draft202012Validator
from smartwatt_analysis.fingerprints import (
    FEATURE_COLUMNS,
    Fingerprint,
    read_fingerprints,
    write_fingerprints,
)

from smartwatt_server.publish_fingerprints import (
    UnpushableTable,
    build_fingerprints_payload,
    publish_fingerprints,
)
from smartwatt_server.wizard import (
    PER_EDGE_QUOTA,
    NoEventToVerify,
    NoTelemetry,
    Step,
    Wizard,
    WrongStep,
)

from .support import CONTRACT, example


@pytest.fixture
def wizard(store, tmp_path):
    return Wizard(store, tmp_path / "fingerprints.csv")


def _conditions(n=1, **conditions):
    return {
        "session_id": "test",
        "background_w": 0.0,
        "concurrent_ids": "",
        "vrms_mean": 240.0,
        "freq_mean": 50.0,
        "notes": f"capture {n}",
        **conditions,
    }


def _telemetry(store, ts, p=12.5, vrms=239.9, freq=50.01, fingerprint_id=None):
    payload = example("telemetry-single-load")
    store.write_telemetry({
        **payload, "ts": ts, "seq": int(ts) % 100000,
        "fingerprint_id": fingerprint_id,
        "electrical": {**payload["electrical"], "p": p, "vrms": vrms, "freq": freq},
    })


def _start(wizard):
    """BASELINE -> QUIET: a baseline is now required (R24), and it is read
    from stored telemetry, so one row is written far from any event."""
    _telemetry(wizard._store, 1.0, p=4.2)  # noqa: SLF001
    wizard.begin()
    wizard.record_baseline()
    assert wizard.advance() is True


def _capture(wizard, label, edge, n=1, **conditions):
    event = example("event-on-confident")
    event = {**event, "label": label, "edge": edge}
    return wizard.capture(label, edge, event, _conditions(n, **conditions))


def _stored_event(store, ts, edge="on", label="kettle", attributed_to="kettle"):
    event = {
        **example("event-on-confident"),
        "ts": ts, "seq": int(ts), "edge": edge,
        "label": label, "attributed_to": attributed_to,
    }
    store.write_event(event)
    return event


def _row(training_id, label, edge, offset, concurrent_ids=""):
    return Fingerprint(
        training_id=training_id, label=label, edge=edge,
        features={
            name: offset + i for i, name in enumerate(FEATURE_COLUMNS)
        },
        ts=1.0, session_id="s", background_w=0.0,
        concurrent_ids=concurrent_ids, vrms_mean=240.0, freq_mean=50.0, notes="",
    )


def _full_quotas(labels, concurrent_ids):
    rows = []
    for c, label in enumerate(labels):
        for edge in ("on", "off"):
            for n in range(PER_EDGE_QUOTA):
                rows.append(_row(f"{label}-{edge}-{concurrent_ids or 'q'}{n}",
                                 label, edge, 100.0 * c + n * 0.37, concurrent_ids))
    return rows


def test_starts_at_baseline(wizard):
    wizard.begin()
    assert wizard.state()["step"] == Step.BASELINE


def test_a_capture_is_not_counted_until_confirmed(wizard, tmp_path):
    """US53: a capture that silently failed and one that succeeded must
    never look the same."""
    _start(wizard)
    result = _capture(wizard, "kettle", "on")
    assert result.accepted is True
    assert wizard.state()["confirmed"] == 0
    assert wizard.state()["awaiting_confirmation"] == 1
    # Nothing on disk either: counts are derived from the file, so an
    # unconfirmed capture must not have written a row.
    assert not (tmp_path / "fingerprints.csv").exists()


def test_confirming_counts_the_capture(wizard):
    _start(wizard)
    result = _capture(wizard, "kettle", "on")
    assert wizard.confirm(result.training_id) is True
    assert wizard.state()["confirmed"] == 1
    assert wizard.state()["breakdown"]["quiet"]["kettle"] == {"on": 1, "off": 0}


def test_confirming_an_unknown_capture_is_false(wizard):
    _start(wizard)
    assert wizard.confirm("nope") is False


def test_cannot_advance_without_the_required_captures(wizard):
    _start(wizard)
    assert wizard.advance() is False
    assert wizard.state()["step"] == Step.QUIET


def test_quotas_are_per_class_and_edge(store, tmp_path):
    """R5: ten captures of ONE class/edge is not the quiet quota, even
    though ten is the whole of that class's quiet total. One class, so a
    quota counted over totals would be met here and advance."""
    wizard = Wizard(store, tmp_path / "f.csv", classes=("a",))
    _start(wizard)
    for n in range(2 * PER_EDGE_QUOTA):
        wizard.confirm(_capture(wizard, "a", "on", n).training_id)
    state = wizard.state()
    assert state["confirmed"] == state["required"] == 2 * PER_EDGE_QUOTA
    assert state["breakdown"]["quiet"]["a"] == {"on": 10, "off": 0}
    assert wizard.advance() is False
    assert wizard.state()["step"] == Step.QUIET


def test_meeting_every_quota_advances(store, tmp_path):
    wizard = Wizard(store, tmp_path / "f.csv", classes=("a", "b"))
    _start(wizard)
    for label in ("a", "b"):
        for edge in ("on", "off"):
            for n in range(PER_EDGE_QUOTA):
                wizard.confirm(_capture(wizard, label, edge, n).training_id)
    state = wizard.state()
    assert state["required"] == 2 * 2 * PER_EDGE_QUOTA
    assert state["confirmed"] == state["required"]
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.OVERLAPPED
    # The quiet rows do not count toward the overlapped quota.
    assert wizard.state()["confirmed"] == 0
    assert wizard.advance() is False


def test_counts_persist_in_the_file(store, tmp_path):
    """R15: a new Wizard over the same file sees what the last one wrote."""
    path = tmp_path / "f.csv"
    write_fingerprints(path, _full_quotas(("a", "b"), ""))
    wizard = Wizard(store, path, classes=("a", "b"))
    _start(wizard)
    assert wizard.state()["confirmed"] == 20
    assert wizard.advance() is True


EVENT_TS = 1754035188.412  # event-on-confident's ts


def test_rows_are_written_with_their_conditions(wizard, store, tmp_path):
    """R24: background_w, vrms_mean and freq_mean are filled SERVER-SIDE from
    the newest stored telemetry at or before the event, within 5 s -- what
    the client sends for them is ignored."""
    _start(wizard)
    _telemetry(store, EVENT_TS - 7.0, p=500.0, vrms=250.0, freq=51.0)  # too old
    _telemetry(store, EVENT_TS - 4.0, p=85.0, vrms=238.7, freq=49.97)  # this one
    _telemetry(store, EVENT_TS + 0.5, p=933.0, vrms=236.0, freq=50.2)  # after
    result = _capture(wizard, "kettle", "on", background_w=999.0,
                      vrms_mean=1.0, freq_mean=2.0, concurrent_ids="desk_fan")
    wizard.confirm(result.training_id)

    rows = read_fingerprints(tmp_path / "fingerprints.csv")
    assert rows[0].background_w == 85.0
    assert rows[0].vrms_mean == 238.7
    assert rows[0].freq_mean == 49.97
    assert rows[0].concurrent_ids == "desk_fan"
    assert rows[0].session_id == "test"
    assert rows[0].notes


def test_no_telemetry_in_the_window_writes_blank_conditions(wizard, store, tmp_path):
    """Nothing measured the conditions: blank cells that read back as None,
    never a fabricated 0 W / 0 V / 0 Hz."""
    _start(wizard)
    _telemetry(store, EVENT_TS - 5.5, p=85.0)  # outside the 5 s window
    result = _capture(wizard, "kettle", "on")
    wizard.confirm(result.training_id)

    text = (tmp_path / "fingerprints.csv").read_text().splitlines()
    cells = dict(zip(text[0].split(","), text[1].split(",")))
    assert (cells["background_w"], cells["vrms_mean"], cells["freq_mean"]) == ("", "", "")
    row = read_fingerprints(tmp_path / "fingerprints.csv")[0]
    assert (row.background_w, row.vrms_mean, row.freq_mean) == (None, None, None)


def test_telemetry_exactly_5_s_before_the_event_is_in_the_window(wizard, store, tmp_path):
    _start(wizard)
    _telemetry(store, EVENT_TS - 5.0, p=77.0)
    wizard.confirm(_capture(wizard, "kettle", "on").training_id)
    assert read_fingerprints(tmp_path / "fingerprints.csv")[0].background_w == 77.0


# -- R24: the baseline is measured, not typed ---------------------------------

def test_record_baseline_reads_the_newest_telemetry(wizard, store):
    _telemetry(store, 10.0, p=3.0)
    _telemetry(store, 11.0, p=4.75)
    wizard.begin()
    assert wizard.record_baseline() == 4.75
    assert wizard.state()["baseline_w"] == 4.75


def test_record_baseline_with_no_telemetry_is_refused(wizard):
    wizard.begin()
    with pytest.raises(NoTelemetry, match="telemetry"):
        wizard.record_baseline()
    assert wizard.state()["baseline_w"] is None


def test_leaving_baseline_requires_a_recorded_baseline(wizard, store):
    wizard.begin()
    assert wizard.advance() is False
    assert "baseline" in wizard.refusal
    assert wizard.state()["step"] == Step.BASELINE
    _telemetry(store, 10.0, p=4.0)
    wizard.record_baseline()
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.QUIET


def test_begin_forgets_the_previous_baseline(wizard, store):
    _telemetry(store, 10.0, p=4.0)
    wizard.begin()
    wizard.record_baseline()
    wizard.begin()
    assert wizard.state()["baseline_w"] is None
    assert wizard.advance() is False


def test_baseline_is_recorded_only_in_the_baseline_step(wizard, store):
    _start(wizard)
    with pytest.raises(WrongStep):
        wizard.record_baseline()


# -- R25: only a clean classifier edge can be captured ------------------------

@pytest.mark.parametrize("reason", ["below_floor", "no_settle", "overlapping_edges"])
def test_an_unusable_event_is_refused_with_its_reason(wizard, store, reason):
    _start(wizard)
    _stored_event(store, EVENT_TS, edge="on")
    store.write_event({
        **example("event-on-confident"), "ts": EVENT_TS + 1, "seq": 9,
        "reason": reason, "ambiguous": reason != "below_floor",
        "rejected": reason == "below_floor",
    })
    result = wizard.capture("kettle", "on", None, _conditions())
    assert result.accepted is False
    assert reason in result.reason
    assert result.event_reason == reason
    assert result.event_ts == EVENT_TS + 1
    assert wizard.state()["awaiting_confirmation"] == 0


@pytest.mark.parametrize("reason", ["below_floor", "no_settle", "overlapping_edges"])
def test_an_unusable_event_passed_directly_is_refused(wizard, reason):
    _start(wizard)
    event = {**example("event-on-confident"), "reason": reason}
    result = wizard.capture("kettle", "on", event, _conditions())
    assert result.accepted is False
    assert reason in result.reason


def test_an_ambiguous_event_without_a_reason_is_still_refused(wizard):
    _start(wizard)
    event = {**example("event-on-confident"), "ambiguous": True}
    result = wizard.capture("kettle", "on", event, _conditions())
    assert result.accepted is False
    assert "ambiguous" in result.reason


def test_a_rejected_but_clean_edge_is_capturable(wizard):
    """distance_threshold is the normal state of an UNTRAINED load -- which
    is exactly what the wizard is there to capture."""
    _start(wizard)
    event = {**example("event-rejected-unknown"), "edge": "on"}
    result = wizard.capture("kettle", "on", event, _conditions())
    assert result.accepted is True
    assert result.event_reason == "distance_threshold"


def test_the_capture_result_describes_the_bound_event(wizard, store):
    _start(wizard)
    _stored_event(store, EVENT_TS, edge="on")
    result = wizard.capture("kettle", "on", None, _conditions())
    assert result.accepted is True
    assert result.event_ts == EVENT_TS
    assert result.delta_p == example("event-on-confident")["features"]["delta_p"]
    assert result.event_reason is None


# -- R26: a capture is confirmable only in the capture step it was made in ---

def test_leaving_a_capture_step_clears_unconfirmed_captures(store, tmp_path):
    path = tmp_path / "f.csv"
    write_fingerprints(path, _full_quotas(("a", "b"), ""))
    wizard = Wizard(store, path, classes=("a", "b"))
    _start(wizard)
    pending = _capture(wizard, "a", "on").training_id
    assert wizard.state()["awaiting_confirmation"] == 1
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.OVERLAPPED
    assert wizard.state()["awaiting_confirmation"] == 0
    assert wizard.confirm(pending) is False
    assert len(wizard.rows()) == 20


def test_confirm_outside_a_capture_step_is_refused(wizard):
    _start(wizard)
    pending = _capture(wizard, "kettle", "on").training_id
    wizard._step = Step.PUSH  # noqa: SLF001
    with pytest.raises(WrongStep):
        wizard.confirm(pending)
    assert wizard.rows() == []


# -- R27c: a malformed hand edit is surfaced, never a crash ------------------

def _malform(path):
    write_fingerprints(path, _full_quotas(("a", "b"), ""))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("hand-001,a,on,1.0,2.0\n")


def test_state_reports_a_malformed_file(store, tmp_path):
    path = tmp_path / "f.csv"
    _malform(path)
    wizard = Wizard(store, path, classes=("a", "b"))
    state = wizard.state()
    assert "line 22" in state["file_error"]
    assert state["breakdown"]["quiet"]["a"] == {"on": 0, "off": 0}


def test_state_file_error_is_null_for_a_good_file(wizard):
    assert wizard.state()["file_error"] is None


def test_capture_and_advance_refuse_on_a_malformed_file(store, tmp_path):
    path = tmp_path / "f.csv"
    wizard = Wizard(store, path, classes=("a", "b"))
    _start(wizard)
    _malform(path)
    result = _capture(wizard, "a", "on")
    assert result.accepted is False
    assert "line 22" in result.reason
    assert wizard.advance() is False
    assert "line 22" in wizard.refusal


def test_overlapped_step_requires_a_concurrent_appliance(wizard):
    """US54: a model trained only against silence fails the moment two
    loads overlap."""
    wizard.begin()
    wizard._step = Step.OVERLAPPED  # noqa: SLF001
    result = _capture(wizard, "kettle", "on", concurrent_ids="")
    assert result.accepted is False
    assert "concurrent" in result.reason.lower()


def test_overlapped_step_accepts_with_a_concurrent_appliance(wizard):
    wizard.begin()
    wizard._step = Step.OVERLAPPED  # noqa: SLF001
    assert _capture(wizard, "kettle", "on", concurrent_ids="desk_fan").accepted


def test_an_untrained_class_is_refused(wizard):
    _start(wizard)
    result = _capture(wizard, "toaster", "on")
    assert result.accepted is False
    assert "toaster" in result.reason


def test_an_event_of_the_other_edge_is_refused(wizard):
    _start(wizard)
    event = {**example("event-on-confident"), "edge": "off"}
    result = wizard.capture("kettle", "on", event, _conditions())
    assert result.accepted is False
    assert "edge" in result.reason


# -- R17: what the firmware loader can hold ----------------------------------

def test_a_label_longer_than_23_characters_is_refused(store, tmp_path):
    long_label = "a" * 24
    wizard = Wizard(store, tmp_path / "f.csv")
    _start(wizard)
    result = _capture(wizard, long_label, "on")
    assert result.accepted is False
    assert "23" in result.reason


def test_a_class_the_firmware_cannot_hold_is_refused_at_construction(store, tmp_path):
    for bad in ("a" * 24, "Kettle", "unknown_5", ""):
        with pytest.raises(ValueError):
            Wizard(store, tmp_path / "f.csv", classes=(bad,))


def test_training_ids_fit_the_firmware_field(store, tmp_path):
    """FingerprintRow::training_id is char[32]: 31 characters, or the
    loader truncates it and the device hashes a different id."""
    longest = "a" * 23
    wizard = Wizard(store, tmp_path / "f.csv", classes=(longest,))
    _start(wizard)
    ids = set()
    for n in range(3):
        result = _capture(wizard, longest, "off", n)
        assert result.accepted
        assert len(result.training_id) <= 31
        ids.add(result.training_id)
        wizard.confirm(result.training_id)
    assert len(ids) == 3
    assert {r.training_id for r in read_fingerprints(tmp_path / "f.csv")} == ids


# -- R8: capture without an event binds the newest stored one ---------------

def test_eventless_capture_binds_the_newest_matching_edge(wizard, store):
    _start(wizard)
    _stored_event(store, 100.0, edge="on")
    _stored_event(store, 101.0, edge="off")
    result = wizard.capture("kettle", "on", None, _conditions())
    assert result.accepted is True
    wizard.confirm(result.training_id)
    assert wizard.rows()[0].ts == 100.0


def test_eventless_capture_never_reuses_an_event(wizard, store):
    _start(wizard)
    _stored_event(store, 100.0, edge="on")
    assert wizard.capture("kettle", "on", None, _conditions()).accepted
    again = wizard.capture("kettle", "on", None, _conditions())
    assert again.accepted is False
    assert again.reason == "no edge detected"


def test_eventless_capture_with_no_event_says_so(wizard):
    _start(wizard)
    result = wizard.capture("kettle", "on", None, _conditions())
    assert result.accepted is False
    assert result.reason == "no edge detected"


def test_eventless_capture_ignores_events_from_before_the_step(wizard, store):
    _stored_event(store, 50.0, edge="on")
    _start(wizard)
    assert wizard.capture("kettle", "on", None, _conditions()).reason == (
        "no edge detected"
    )


# -- the verification gate --------------------------------------------------

def test_verification_needs_three_in_a_row(wizard):
    """US55: setup is not declared complete until it demonstrably works."""
    wizard.begin()
    wizard._step = Step.VERIFY  # noqa: SLF001
    assert wizard.verify("kettle", "kettle")["streak"] == 1
    assert wizard.verify("desk_fan", "desk_fan")["streak"] == 2
    result = wizard.verify("led_bulb", "led_bulb")
    assert result["streak"] == 3
    assert result["passed"] is True


def test_a_wrong_answer_resets_the_streak(wizard):
    wizard.begin()
    wizard._step = Step.VERIFY  # noqa: SLF001
    wizard.verify("kettle", "kettle")
    wizard.verify("desk_fan", "desk_fan")
    result = wizard.verify("led_bulb", "laptop_charger")
    assert result["streak"] == 0
    assert result["passed"] is False


def test_verification_is_a_gate_not_a_summary(wizard):
    """Failing it returns to capture rather than completing with a warning."""
    wizard.begin()
    wizard._step = Step.VERIFY  # noqa: SLF001
    wizard.verify("kettle", "desk_fan")
    assert wizard.state()["step"] == Step.QUIET
    assert wizard.state()["streak"] == 0


def test_failing_verification_keeps_the_quotas_already_met(store, tmp_path):
    """R15: back to capture, not back to zero."""
    path = tmp_path / "f.csv"
    write_fingerprints(path, _full_quotas(("a", "b"), "")
                       + _full_quotas(("a", "b"), "fan"))
    wizard = Wizard(store, path, classes=("a", "b"))
    wizard.begin()
    wizard._step = Step.VERIFY  # noqa: SLF001
    wizard.verify("a", "b")
    assert wizard.advance() is True
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.PUSH


def test_passing_verification_completes(wizard):
    wizard.begin()
    wizard._step = Step.VERIFY  # noqa: SLF001
    for label in ("kettle", "desk_fan", "led_bulb"):
        wizard.verify(label, label)
    assert wizard.state()["step"] == Step.DONE


def test_verify_outside_the_verify_step_is_refused(wizard):
    wizard.begin()
    with pytest.raises(WrongStep):
        wizard.verify("kettle", "kettle")


def test_verify_without_an_answer_reads_the_newest_event(wizard, store):
    """R14: attributed_to first, falling back to label."""
    wizard.begin()
    wizard._step = Step.VERIFY  # noqa: SLF001
    _stored_event(store, 10.0, label="desk_fan", attributed_to="kettle")
    assert wizard.verify("kettle")["actual"] == "kettle"
    _stored_event(store, 11.0, label="desk_fan", attributed_to=None)
    assert wizard.verify("desk_fan")["actual"] == "desk_fan"


def test_verify_without_an_answer_never_reuses_an_event(wizard, store):
    wizard.begin()
    wizard._step = Step.VERIFY  # noqa: SLF001
    _stored_event(store, 10.0)
    wizard.verify("kettle")
    with pytest.raises(NoEventToVerify):
        wizard.verify("kettle")


def test_verify_ignores_events_from_before_verification(store, tmp_path):
    wizard = _to_push_step(store=store, tmp_path=tmp_path)
    assert wizard.state()["step"] == Step.PUSH
    # Stored while still in PUSH -- BEFORE the watermark is taken.
    _stored_event(store, 10.0, label="a", attributed_to="a")
    _device_reports(wizard, store)
    wizard.advance()
    assert wizard.state()["step"] == Step.VERIFY
    with pytest.raises(NoEventToVerify):
        wizard.verify("a")


# -- R21/R22: the push is attempted, but the DEVICE's id is the gate --------

def _flat_quota_rows(labels, concurrent_ids):
    """Like `_full_quotas`, but every row of one class has IDENTICAL
    features (no `n`-dependent offset) -- zero within-class spread, so a
    single-class table built from this derives a threshold of 0 and is
    refused (`test_a_single_class_table_is_refused_not_pushed`'s own
    fixture shape, reused here through `Wizard.advance()` instead of
    `build_fingerprints_payload` directly)."""
    rows = []
    for c, label in enumerate(labels):
        for edge in ("on", "off"):
            for n in range(PER_EDGE_QUOTA):
                rows.append(_row(f"{label}-{edge}-{concurrent_ids or 'q'}{n}",
                                 label, edge, 100.0 * c, concurrent_ids))
    return rows


def _to_push_step(classes=("a", "b"), *, store, tmp_path, publisher=None,
                  rows_fn=_full_quotas):
    """Quotas already met on disk; baseline, then two advance() calls,
    reach PUSH."""
    path = tmp_path / "f.csv"
    write_fingerprints(
        path, rows_fn(classes, "") + rows_fn(classes, "fan"),
    )
    wizard = Wizard(store, path, classes=classes, publisher=publisher)
    _start(wizard)
    wizard.advance()
    wizard.advance()
    return wizard


def _table_id(wizard) -> str:
    """Independently of the wizard: the id of the table on disk."""
    return build_fingerprints_payload(wizard.rows())["fingerprint_id"]


_DEVICE_TS = [100.0]


def _device_reports(wizard, store, fingerprint_id=None):
    """The device's telemetry, newest, reporting a table id (default: the
    one the wizard built)."""
    _DEVICE_TS[0] += 1.0
    _telemetry(store, _DEVICE_TS[0],
               fingerprint_id=fingerprint_id or _table_id(wizard))


def test_state_carries_no_push_outcome_before_any_attempt(wizard):
    wizard.begin()
    assert wizard.state()["push"] is None


def test_state_carries_the_push_outcome_while_in_push(store, tmp_path):
    wizard = _to_push_step(store=store, tmp_path=tmp_path)
    state = wizard.state()
    assert state["step"] == Step.PUSH
    assert state["push"] == {
        "published": False, "fingerprint_id": None, "reason": None,
    }


def test_state_in_push_carries_the_expected_and_the_device_id(store, tmp_path):
    wizard = _to_push_step(store=store, tmp_path=tmp_path)
    state = wizard.state()
    assert state["expected_fingerprint_id"] == _table_id(wizard)
    assert state["device_fingerprint_id"] is None
    _device_reports(wizard, store, "0badc0de")
    assert wizard.state()["device_fingerprint_id"] == "0badc0de"


def test_a_published_push_is_not_the_gate(store, tmp_path):
    """R22: the broker taking the message says nothing about the device.
    Published, but no telemetry from the device: still PUSH."""
    wizard = _to_push_step(store=store, tmp_path=tmp_path,
                           publisher=lambda topic, payload, retain: True)
    assert wizard.state()["push"]["published"] is True
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.PUSH


def test_push_stays_while_the_device_reports_a_different_id(store, tmp_path):
    wizard = _to_push_step(store=store, tmp_path=tmp_path,
                           publisher=lambda topic, payload, retain: True)
    _device_reports(wizard, store, "0badc0de")
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.PUSH
    assert wizard.state()["device_fingerprint_id"] == "0badc0de"


def test_push_reaches_verify_once_the_device_reports_the_table(store, tmp_path):
    wizard = _to_push_step(store=store, tmp_path=tmp_path,
                           publisher=lambda topic, payload, retain: True)
    _device_reports(wizard, store)
    assert wizard.advance() is True
    state = wizard.state()
    assert state["step"] == Step.VERIFY
    # Kept visible in VERIFY too.
    assert state["expected_fingerprint_id"] == _table_id(wizard)
    assert state["device_fingerprint_id"] == _table_id(wizard)


def test_only_the_newest_telemetry_counts(store, tmp_path):
    """An older row that once matched does not stand in for what the
    device reports now."""
    wizard = _to_push_step(store=store, tmp_path=tmp_path,
                           publisher=lambda topic, payload, retain: True)
    _device_reports(wizard, store)
    _device_reports(wizard, store, "0badc0de")
    wizard.advance()
    assert wizard.state()["step"] == Step.PUSH


def test_an_uploadfs_table_passes_the_gate_without_mqtt(store, tmp_path):
    """The device has no fingerprint subscriber yet (R7): the table reaches
    it by `pio run -t uploadfs`. With no MQTT client at all, a device that
    reports the table's id still passes -- the id is the whole gate."""
    wizard = _to_push_step(store=store, tmp_path=tmp_path)
    assert wizard.state()["push"]["published"] is False
    _device_reports(wizard, store)
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.VERIFY


def test_advance_from_push_with_no_publisher_stays_in_push(store, tmp_path):
    wizard = _to_push_step(store=store, tmp_path=tmp_path)
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.PUSH
    assert wizard.state()["push"]["published"] is False


def test_advance_retries_an_unpublished_push(store, tmp_path):
    """The retry case: the first attempt (no publisher yet) leaves the
    wizard in PUSH, unpublished; wiring in a working publisher and advancing
    again publishes. The device's id then moves it on."""
    wizard = _to_push_step(store=store, tmp_path=tmp_path)
    assert wizard.state()["push"]["published"] is False
    wizard._publisher = lambda topic, payload, retain: True  # noqa: SLF001
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.PUSH
    assert wizard.state()["push"]["published"] is True
    _device_reports(wizard, store)
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.VERIFY
    # Left PUSH: state() no longer surfaces the push attempt itself...
    assert wizard.state()["push"] is None
    # ...but the wizard still knows what just happened, which is what lets
    # api.py mirror it into THIS response before moving on.
    assert wizard.last_push_result() == {
        "published": True, "fingerprint_id": _table_id(wizard), "reason": None,
    }


def test_an_unpushable_table_reports_its_reason_while_still_in_push(
    store, tmp_path,
):
    """One class, no spread: derive_threshold is 0, refused before ever
    reaching the publisher -- and retried on every later advance() call
    exactly like a broker refusal would be. There is no expected id, so no
    device report can pass the gate."""
    wizard = _to_push_step(("a",), store=store, tmp_path=tmp_path,
                           rows_fn=_flat_quota_rows)
    state = wizard.state()
    assert state["step"] == Step.PUSH
    assert state["push"]["published"] is False
    assert "threshold" in state["push"]["reason"]
    assert state["expected_fingerprint_id"] is None
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.PUSH


def test_returning_to_capture_clears_a_stale_push_outcome(store, tmp_path):
    """A push already attempted belongs to the table as it stood before a
    failed verification sent the wizard back to capture -- more captures
    change the rows, so a later PUSH must attempt again, not skip straight
    to VERIFY on a stale success."""
    wizard = _to_push_step(store=store, tmp_path=tmp_path,
                           publisher=lambda topic, payload, retain: True)
    _device_reports(wizard, store)
    assert wizard.advance() is True
    assert wizard.state()["step"] == Step.VERIFY
    wizard.verify("a", "b")  # wrong answer: back to capture (R15)
    assert wizard.state()["step"] == Step.QUIET
    assert wizard.last_push_result() is None


def test_a_changed_table_is_pushed_again(store, tmp_path):
    """The file changed after a push (a hand edit): the published table is
    not the one on disk any more, so the next advance publishes again."""
    sent = []
    wizard = _to_push_step(
        store=store, tmp_path=tmp_path,
        publisher=lambda topic, payload, retain: sent.append(payload) or True,
    )
    assert len(sent) == 1
    rows = wizard.rows()
    rows[0].features["delta_p"] += 1.0
    write_fingerprints(wizard._path, rows)  # noqa: SLF001
    wizard.advance()
    assert len(sent) == 2
    assert json.loads(sent[1])["fingerprint_id"] == _table_id(wizard)
    assert wizard.state()["step"] == Step.PUSH


# -- the fingerprint push ---------------------------------------------------

def test_payload_validates_against_the_schema(tmp_path):
    rows = [
        Fingerprint(
            training_id=f"kettle-on-{n:03d}", label="kettle", edge="on",
            features={name: float(i + n) for i, name in enumerate(FEATURE_COLUMNS)},
            ts=1754035200.0 + n, session_id="s", background_w=0.0,
            concurrent_ids="", vrms_mean=240.0, freq_mean=50.0, notes="",
        )
        for n in range(1, 6)
    ]
    payload = build_fingerprints_payload(rows)
    schema = json.loads((CONTRACT / "schemas" / "fingerprints.schema.json").read_text())
    Draft202012Validator(schema).validate(payload)


def test_payload_carries_a_derived_threshold(tmp_path):
    rows = []
    for label, offset in (("a", 0.0), ("b", 100.0)):
        for n in range(1, 6):
            rows.append(Fingerprint(
                training_id=f"{label}-on-{n:03d}", label=label, edge="on",
                features={
                    name: offset + (0.1 * n if i == 0 else float(i))
                    for i, name in enumerate(FEATURE_COLUMNS)
                },
                ts=float(n), session_id="s", background_w=0.0,
                concurrent_ids="", vrms_mean=240.0, freq_mean=50.0, notes="",
            ))
    payload = build_fingerprints_payload(rows)
    assert payload["rejection_threshold"] > 0


def test_fingerprint_id_changes_when_a_row_changes(tmp_path):
    # Two classes: a single-class table with no spread is refused outright
    # (see test_a_single_class_table_is_refused_not_pushed).
    rows = _full_quotas(("a", "b"), "")
    a = build_fingerprints_payload(rows)["fingerprint_id"]
    rows[3].features["delta_q1"] += 0.5
    b = build_fingerprints_payload(rows)["fingerprint_id"]
    assert a != b


def test_fingerprint_id_changes_on_a_relabel_alone():
    """The firmware hashes labels for exactly this correction."""
    rows = _full_quotas(("a", "b"), "")
    before = build_fingerprints_payload(rows)["fingerprint_id"]
    rows[0].label = "b"
    assert build_fingerprints_payload(rows)["fingerprint_id"] != before


def test_fingerprint_id_follows_file_order():
    """Rows are hashed in file order, as the device reads them."""
    rows = _full_quotas(("a", "b"), "")
    forward = build_fingerprints_payload(rows)["fingerprint_id"]
    assert build_fingerprints_payload(rows[::-1])["fingerprint_id"] != forward


def test_a_single_class_table_is_refused_not_pushed():
    """derive_threshold is 0 for a single class with no spread, which the
    schema's exclusiveMinimum forbids and the device refuses to load."""
    rows = [
        Fingerprint(
            training_id=f"a-on-{n:03d}", label="a", edge="on",
            features={name: float(i) for i, name in enumerate(FEATURE_COLUMNS)},
            ts=float(n), session_id="s", background_w=0.0,
            concurrent_ids="", vrms_mean=240.0, freq_mean=50.0, notes="",
        )
        for n in range(1, 4)
    ]
    with pytest.raises(UnpushableTable, match="threshold"):
        build_fingerprints_payload(rows)


def test_an_empty_table_is_refused():
    with pytest.raises(UnpushableTable):
        build_fingerprints_payload([])


def test_a_row_the_firmware_would_skip_is_refused():
    """A row the loader drops is a row the device does not hash, so the
    ids could never match: refuse rather than push a table that will
    always look stale."""
    for field, value in (("label", "Kettle"), ("label", "b" * 24),
                         ("training_id", "x" * 32)):
        rows = _full_quotas(("a", "b"), "")
        setattr(rows[0], field, value)
        with pytest.raises(UnpushableTable):
            build_fingerprints_payload(rows)


def test_publish_uses_the_retained_topic():
    sent = []
    payload = {"schema": "smartwatt.fingerprints.v1", "fingerprint_id": "abcd1234"}
    publish_fingerprints(lambda t, p, retain: sent.append((t, retain)), payload)
    assert sent == [("smartwatt/fingerprints", True)]


def test_publish_reports_what_the_publisher_reported():
    payload = {"schema": "smartwatt.fingerprints.v1", "fingerprint_id": "abcd1234"}
    assert publish_fingerprints(lambda t, p, retain: True, payload) is True
    assert publish_fingerprints(lambda t, p, retain: False, payload) is False
    # A publisher that says nothing has not said it succeeded.
    assert publish_fingerprints(lambda t, p, retain: None, payload) is False
