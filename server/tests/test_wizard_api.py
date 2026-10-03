"""The wizard endpoints: GET /api/wizard, POST /api/wizard/{begin,
capture,confirm,advance,verify}, GET /api/wizard/scatter.

As in test_control_api.py, every assertion checks response CONTENT: a
route shadowed by the SPA catch-all comes back as a 404 dict, which only a
content check catches.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from smartwatt_analysis.fingerprints import (
    FEATURE_COLUMNS,
    Fingerprint,
    read_fingerprints,
    write_fingerprints,
)

from smartwatt_server.api import create_app
from smartwatt_server.config import settings as load_settings
from smartwatt_server.ingest import MqttIngest
from smartwatt_server.publish_fingerprints import TOPIC, build_fingerprints_payload
from smartwatt_server.wizard import PER_EDGE_QUOTA, Step

from .support import example


def _settings(tmp_path, classes=("a", "b")):
    return dataclasses.replace(
        load_settings(),
        trained_classes=classes,
        fingerprints_path=tmp_path / "fingerprints.csv",
    )


@pytest.fixture
def client(store, tmp_path):
    app = create_app(store=store, settings=_settings(tmp_path), start_ingest=False)
    with TestClient(app) as test_client:
        yield test_client


def _event(ts=1754035188.412, edge="on", **overrides):
    return {**example("event-on-confident"), "ts": ts, "seq": int(ts) % 100000,
            "edge": edge, **overrides}


def _conditions(**overrides):
    """What the client may send: session_id, concurrent_ids, notes (R24)."""
    return {"session_id": "api", "concurrent_ids": "", "notes": "", **overrides}


def _telemetry(store, ts, p=12.5, vrms=239.9, freq=50.01, fingerprint_id=None):
    payload = example("telemetry-single-load")
    store.write_telemetry({
        **payload, "ts": ts, "seq": int(ts) % 100000,
        "fingerprint_id": fingerprint_id,
        "electrical": {**payload["electrical"], "p": p, "vrms": vrms, "freq": freq},
    })


def _quota_rows(labels, spread=0.37):
    rows = []
    for c, label in enumerate(labels):
        for concurrent in ("", "fan"):
            for edge in ("on", "off"):
                for n in range(PER_EDGE_QUOTA):
                    rows.append(Fingerprint(
                        training_id=f"{label}-{edge}-{concurrent or 'q'}{n}",
                        label=label, edge=edge,
                        features={
                            name: 100.0 * c + spread * n + i
                            for i, name in enumerate(FEATURE_COLUMNS)
                        },
                        ts=float(n), session_id="s", background_w=0.0,
                        concurrent_ids=concurrent, vrms_mean=240.0,
                        freq_mean=50.0, notes="",
                    ))
    return rows


def _to_capture(client):
    _telemetry(client.app.state.store, 1.0, p=4.2)
    assert client.post("/api/wizard/begin").json()["step"] == "baseline"
    assert client.post("/api/wizard/baseline").json()["baseline_w"] == 4.2
    assert client.post("/api/wizard/advance").json()["step"] == "quiet"


def test_state_is_served(client):
    body = client.get("/api/wizard").json()
    assert body["step"] == "baseline"
    assert body["classes"] == ["a", "b"]


def test_capture_then_confirm(client, store, tmp_path):
    _to_capture(client)
    store.write_event(_event())
    captured = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "conditions": _conditions(),
    }).json()
    assert captured["accepted"] is True
    assert captured["reason"] is None
    assert client.get("/api/wizard").json()["confirmed"] == 0

    state = client.post("/api/wizard/confirm",
                        json={"training_id": captured["training_id"]}).json()
    assert state["confirmed"] == 1
    assert state["breakdown"]["quiet"]["a"] == {"on": 1, "off": 0}
    rows = read_fingerprints(tmp_path / "fingerprints.csv")
    assert [r.training_id for r in rows] == [captured["training_id"]]


def test_confirming_an_unknown_capture_is_404(client):
    _to_capture(client)
    response = client.post("/api/wizard/confirm", json={"training_id": "nope"})
    assert response.status_code == 404
    assert response.json()["detail"] == "no such capture"


def test_an_early_advance_is_409(client):
    _to_capture(client)
    response = client.post("/api/wizard/advance")
    assert response.status_code == 409
    assert "required captures" in response.json()["detail"]
    assert client.get("/api/wizard").json()["step"] == "quiet"


def test_eventless_capture_with_nothing_stored(client):
    _to_capture(client)
    body = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "conditions": _conditions(),
    }).json()
    assert body["accepted"] is False
    assert body["reason"] == "no edge detected"


def test_eventless_capture_binds_the_stored_event(client, store):
    _to_capture(client)
    store.write_event(_event(ts=2000.0, edge="on"))
    body = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "conditions": _conditions(),
    }).json()
    assert body["accepted"] is True
    client.post("/api/wizard/confirm", json={"training_id": body["training_id"]})
    assert client.app.state.wizard.rows()[0].ts == 2000.0


def _to_push(client, tmp_path, labels=("a", "b"), spread=0.37):
    write_fingerprints(tmp_path / "fingerprints.csv", _quota_rows(labels, spread))
    _to_capture(client)
    assert client.post("/api/wizard/advance").json()["step"] == "overlapped"
    return client.post("/api/wizard/advance")


def test_push_without_mqtt_does_not_claim_success(client, tmp_path):
    response = _to_push(client, tmp_path)
    body = response.json()
    assert body["step"] == "push"
    assert body["published"] is False
    assert "fingerprint_id" not in body


def test_an_unpushable_table_is_refused(store, tmp_path):
    """One class with no spread: derive_threshold is 0, which the schema
    forbids and the device's loader refuses."""
    app = create_app(store=store, settings=_settings(tmp_path, classes=("a",)),
                     start_ingest=False)
    with TestClient(app) as client:
        body = _to_push(client, tmp_path, labels=("a",), spread=0.0).json()
    assert body["published"] is False
    assert "threshold" in body["push_refused"]


def test_push_through_a_real_client_is_retained(store, tmp_path, monkeypatch):
    """The production wiring: a real MqttIngest from the lifespan, with a
    stub standing in for the paho client its thread would build."""
    sent = []

    class _Paho:
        def publish(self, topic, payload, retain=False):
            sent.append((topic, json.loads(payload), retain))
            return SimpleNamespace(rc=0)

        def disconnect(self):
            pass

    monkeypatch.setattr(MqttIngest, "start", lambda self: None)
    app = create_app(store=store, settings=_settings(tmp_path), start_ingest=True)
    with TestClient(app) as client:
        client.app.state.mqtt._client = _Paho()  # noqa: SLF001
        body = _to_push(client, tmp_path).json()

    expected = build_fingerprints_payload(_quota_rows(("a", "b")))
    assert body["published"] is True
    assert body["fingerprint_id"] == expected["fingerprint_id"]
    assert sent == [(TOPIC, expected, True)]


def test_push_reports_a_broker_refusal(store, tmp_path, monkeypatch):
    class _Paho:
        def publish(self, topic, payload, retain=False):
            return SimpleNamespace(rc=4)  # MQTT_ERR_NO_CONN

        def disconnect(self):
            pass

    monkeypatch.setattr(MqttIngest, "start", lambda self: None)
    app = create_app(store=store, settings=_settings(tmp_path), start_ingest=True)
    with TestClient(app) as client:
        client.app.state.mqtt._client = _Paho()  # noqa: SLF001
        body = _to_push(client, tmp_path).json()
    assert body["published"] is False


def test_get_wizard_carries_the_push_outcome(client, tmp_path):
    """R21: the panel must not depend on catching the one advance()
    response -- a plain GET /api/wizard, taken any time afterward while
    still in PUSH, carries the same outcome."""
    _to_push(client, tmp_path)
    state = client.get("/api/wizard").json()
    assert state["step"] == "push"
    assert state["push"] == {
        "published": False, "fingerprint_id": None, "reason": None,
    }
    expected = build_fingerprints_payload(_quota_rows(("a", "b")))
    assert state["expected_fingerprint_id"] == expected["fingerprint_id"]
    assert state["device_fingerprint_id"] is None


def test_advance_retries_a_push_that_did_not_publish(store, tmp_path, monkeypatch):
    """R21: a later advance() call, made while still in PUSH, re-attempts
    the push rather than moving on regardless -- wiring in a working
    client between the two calls is what makes the SECOND one succeed."""
    class _Paho:
        def __init__(self):
            self.connected = False

        def publish(self, topic, payload, retain=False):
            if not self.connected:
                return SimpleNamespace(rc=4)  # MQTT_ERR_NO_CONN
            return SimpleNamespace(rc=0)

        def disconnect(self):
            pass

    monkeypatch.setattr(MqttIngest, "start", lambda self: None)
    app = create_app(store=store, settings=_settings(tmp_path), start_ingest=True)
    with TestClient(app) as client:
        paho = _Paho()
        client.app.state.mqtt._client = paho  # noqa: SLF001
        first = _to_push(client, tmp_path).json()
        assert first["step"] == "push"
        assert first["published"] is False

        paho.connected = True
        second = client.post("/api/wizard/advance").json()

    expected = build_fingerprints_payload(_quota_rows(("a", "b")))
    assert second["published"] is True
    assert second["fingerprint_id"] == expected["fingerprint_id"]
    # R22: published, but the device has not reported the table: still PUSH.
    assert second["step"] == "push"
    assert second["expected_fingerprint_id"] == expected["fingerprint_id"]
    assert second["device_fingerprint_id"] is None


def test_verify_reads_a_correct_answer_from_the_device(client, store):
    client.app.state.wizard._step = Step.VERIFY  # noqa: SLF001
    store.write_event(_event(ts=3000.0, label="a", attributed_to="a"))
    body = client.post("/api/wizard/verify", json={"expected": "a"}).json()
    assert body == {"expected": "a", "actual": "a", "correct": True,
                    "streak": 1, "required": 3, "passed": False}


def test_a_client_supplied_actual_is_ignored(client):
    """R27b: the answer is the DEVICE's. A body carrying `actual` does not
    get to supply it: with no event stored, it is still a 409."""
    client.app.state.wizard._step = Step.VERIFY  # noqa: SLF001
    response = client.post("/api/wizard/verify",
                           json={"expected": "a", "actual": "a"})
    assert response.status_code == 409
    assert "no event" in response.json()["detail"]


def test_verify_reads_the_newest_event(client, store):
    client.app.state.wizard._step = Step.VERIFY  # noqa: SLF001
    store.write_event(_event(ts=3000.0, label="b", attributed_to="b"))
    body = client.post("/api/wizard/verify", json={"expected": "a"}).json()
    assert body["actual"] == "b"
    assert body["correct"] is False
    assert client.get("/api/wizard").json()["step"] == "quiet"


def test_verify_with_no_event_is_409(client):
    client.app.state.wizard._step = Step.VERIFY  # noqa: SLF001
    response = client.post("/api/wizard/verify", json={"expected": "a"})
    assert response.status_code == 409
    assert "no event" in response.json()["detail"]


def test_verify_outside_the_verify_step_is_409(client):
    response = client.post("/api/wizard/verify", json={"expected": "a"})
    assert response.status_code == 409
    assert "verify" in response.json()["detail"]


def test_scatter(client, tmp_path):
    assert client.get("/api/wizard/scatter").json() == {"points": [], "labels": []}
    write_fingerprints(tmp_path / "fingerprints.csv", _quota_rows(("a", "b")))
    body = client.get("/api/wizard/scatter").json()
    assert len(body["points"]) == 40
    assert body["labels"] == ["a", "b"]


# -- R22: VERIFY only once the device reports the table ----------------------

def test_advance_reaches_verify_only_when_the_device_reports_the_table(
    client, store, tmp_path,
):
    body = _to_push(client, tmp_path).json()
    assert body["step"] == "push"
    expected = build_fingerprints_payload(_quota_rows(("a", "b")))["fingerprint_id"]

    _telemetry(store, 500.0, fingerprint_id="0badc0de")
    body = client.post("/api/wizard/advance").json()
    assert body["step"] == "push"
    assert body["device_fingerprint_id"] == "0badc0de"
    assert body["expected_fingerprint_id"] == expected

    _telemetry(store, 501.0, fingerprint_id=expected)
    body = client.post("/api/wizard/advance").json()
    assert body["step"] == "verify"
    state = client.get("/api/wizard").json()
    assert state["expected_fingerprint_id"] == expected
    assert state["device_fingerprint_id"] == expected


# -- R24: the baseline and the conditions are measured -----------------------

def test_baseline_with_no_telemetry_is_409(client):
    client.post("/api/wizard/begin")
    response = client.post("/api/wizard/baseline")
    assert response.status_code == 409
    assert "telemetry" in response.json()["detail"]
    assert client.get("/api/wizard").json()["baseline_w"] is None


def test_advance_out_of_baseline_needs_a_baseline(client):
    client.post("/api/wizard/begin")
    response = client.post("/api/wizard/advance")
    assert response.status_code == 409
    assert "baseline" in response.json()["detail"]
    assert client.get("/api/wizard").json()["step"] == "baseline"


def test_capture_conditions_come_from_telemetry_not_the_client(client, store):
    _to_capture(client)
    store.write_event(_event(ts=2000.0))
    _telemetry(store, 1998.0, p=85.0, vrms=238.7, freq=49.97)
    captured = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on",
        "conditions": _conditions(background_w=999.0, vrms_mean=1.0, freq_mean=2.0),
    }).json()
    client.post("/api/wizard/confirm", json={"training_id": captured["training_id"]})
    row = client.app.state.wizard.rows()[0]
    assert (row.background_w, row.vrms_mean, row.freq_mean) == (85.0, 238.7, 49.97)
    assert row.session_id == "api"


def test_capture_with_no_telemetry_in_the_window_writes_none(client, store):
    _to_capture(client)
    store.write_event(_event(ts=2000.0))
    captured = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "conditions": _conditions(),
    }).json()
    client.post("/api/wizard/confirm", json={"training_id": captured["training_id"]})
    row = client.app.state.wizard.rows()[0]
    assert (row.background_w, row.vrms_mean, row.freq_mean) == (None, None, None)


# -- R25 / R27b: the capture binds the device's event, and describes it -------

def test_a_client_supplied_event_is_ignored(client):
    """R27b: `event` is not part of the request. Sent anyway, it is not
    used: nothing is stored, so nothing is bound."""
    _to_capture(client)
    body = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "event": _event(), "conditions": _conditions(),
    }).json()
    assert body["accepted"] is False
    assert body["reason"] == "no edge detected"


def test_capture_returns_the_bound_events_description(client, store):
    _to_capture(client)
    store.write_event(_event(ts=2000.0))
    body = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "conditions": _conditions(),
    }).json()
    assert body["event_ts"] == 2000.0
    assert body["delta_p"] == example("event-on-confident")["features"]["delta_p"]
    assert body["event_reason"] is None


def test_capture_of_an_overlapping_edge_is_refused_with_its_reason(client, store):
    _to_capture(client)
    store.write_event(_event(ts=2000.0, reason="overlapping_edges", ambiguous=True))
    body = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "conditions": _conditions(),
    }).json()
    assert body["accepted"] is False
    assert "overlapping_edges" in body["reason"]
    assert body["event_reason"] == "overlapping_edges"
    assert body["event_ts"] == 2000.0


# -- R26 ----------------------------------------------------------------------

def test_confirm_outside_a_capture_step_is_409(client, store):
    _to_capture(client)
    store.write_event(_event(ts=2000.0))
    captured = client.post("/api/wizard/capture", json={
        "label": "a", "edge": "on", "conditions": _conditions(),
    }).json()
    client.app.state.wizard._step = Step.PUSH  # noqa: SLF001
    response = client.post("/api/wizard/confirm",
                           json={"training_id": captured["training_id"]})
    assert response.status_code == 409
    assert client.app.state.wizard.rows() == []


# -- R27c: a malformed hand edit is a stated error, not a 500 ----------------

def _malform(tmp_path):
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, _quota_rows(("a", "b")))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("hand-001,a,on,1.0,,3.0\n")


def test_get_wizard_reports_a_malformed_file(client, tmp_path):
    _malform(tmp_path)
    response = client.get("/api/wizard")
    assert response.status_code == 200
    assert "line 42" in response.json()["file_error"]


def test_scatter_reports_a_malformed_file(client, tmp_path):
    _malform(tmp_path)
    response = client.get("/api/wizard/scatter")
    assert response.status_code == 200
    body = response.json()
    assert body["points"] == []
    assert "line 42" in body["error"]


def test_scatter_error_is_absent_for_a_good_file(client, tmp_path):
    write_fingerprints(tmp_path / "fingerprints.csv", _quota_rows(("a", "b")))
    assert "error" not in client.get("/api/wizard/scatter").json()
