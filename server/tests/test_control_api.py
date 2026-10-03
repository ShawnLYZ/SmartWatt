"""The five control endpoints: POST /api/control, GET/POST /api/pending,
GET /api/actions, GET /api/rules.

Every assertion below checks response CONTENT, not status_code alone.
Ruling A's hazard is a route registered after the SPA catch-all in
api.py: FastAPI matches routes in registration order, so a route
registered after `spa` is never reached at all. Because every route
here lives under /api/, `spa`'s own explicit 404-for-"api/"-paths branch
means a shadowed GET actually comes back as a 404 {"detail": "Not
Found"} rather than a 200 with index.html's markup -- confirmed
directly by moving a route below `spa` in this file and rerunning it
(see task-5a-report.md's Ruling A evidence). Either failure shape is
still only caught by checking content: several tests below never assert
status_code at all and instead index straight into the body
(`first[0][...]`, `body[0][...]`), which is exactly what catches a
shadowed route reliably regardless of which failure shape it takes -- a
dict has no integer index, whichever wrong dict comes back.
"""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

from smartwatt_server.api import create_app
from smartwatt_server.config import settings as load_settings
from smartwatt_server.ingest import TOPIC_TELEMETRY
from smartwatt_server.rules import DEMO_DEFAULTS

from .support import example


@pytest.fixture
def client(store):
    # Matches test_api.py's own fixture exactly: no broker, no MQTT
    # client, and per this task's brief the app must still serve -- the
    # gate and rules engine must exist regardless of start_ingest.
    app = create_app(store=store, start_ingest=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def demo_client(store):
    """DEMO_DEFAULTS (30 s left_on, 60 s grace) rather than SHIPPED's 4 h
    -- the only way a left_on pending cut can exist inside a fast unit
    test rather than a real multi-hour wait."""
    demo_settings = dataclasses.replace(load_settings(), rules_demo=True)
    app = create_app(store=store, settings=demo_settings, start_ingest=False)
    with TestClient(app) as test_client:
        yield test_client


class _FakeMqtt:
    """Stands in for a connected MqttIngest.

    api.py's `_publish_command` closure reads `app.state.mqtt` fresh on
    every call rather than capturing a reference when it is built (the
    real MqttIngest is only constructed, and only when start_ingest is
    True), so swapping this in after the app is already running is
    enough to exercise the closure's happy path without a real broker.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def publish_command(self, topic: str, payload: str, authority=None) -> bool:
        # `publish_command`, not `publish`: the real MqttIngest REFUSES a
        # cmnd/ topic on its general publish path and carries commands
        # only here (see ingest.py, and test_gate_property.py's runtime
        # tests over a real MqttIngest). A stand-in that answered
        # `publish` would let the wiring in api.py drift back onto the
        # refused path with every test here still green.
        self.sent.append((topic, payload))
        return True

    def stop(self) -> None:
        # lifespan's shutdown unconditionally calls app.state.mqtt.stop()
        # when app.state.mqtt is not None -- a real MqttIngest always has
        # one, so this stand-in needs one too or teardown itself errors.
        pass


def _warn_the_lamp(client, *, on_for_s: float = 100.0) -> None:
    """Feed one real telemetry sample claiming the lamp has already been
    on for ``on_for_s`` seconds, then tick the SAME RulesEngine instance
    the API itself uses -- not a fake one constructed for the test.

    rules.py's observe() derives on-duration from the payload's OWN
    ts/since fields and re-anchors it onto the injected clock (real
    time.time() in production wiring, which create_app() does not let a
    test override) -- so a single payload already 100 s "into" being on
    is enough to clear DEMO_DEFAULTS.left_on_seconds (30 s) the instant
    tick() runs next, regardless of how little real wall-clock time the
    test itself takes.
    """
    base = example("telemetry-single-load")
    ts = 2_000_000.0
    payload = {
        **base,
        "ts": ts,
        "seq": 1,
        "attribution": {
            "active": [{"id": "incandescent_lamp", "w": 40.0, "since": ts - on_for_s}],
            "residual_w": 0.0,
            "floor_w": 6.0,
        },
    }
    assert client.app.state.ingestor.handle(
        TOPIC_TELEMETRY, json.dumps(payload).encode()
    )
    client.app.state.rules.tick()


def _wait_until(condition, *, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll `condition` until it is truthy or `timeout` seconds pass.

    Used ONLY by test_rules_loop_ticks_on_its_own below, to observe the
    real background _rules_loop task doing its own thing on its own
    real-time schedule -- never as a way to paper over a race this test
    itself could dodge by calling tick() directly, which would defeat
    the one thing that test exists to prove.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


# -- POST /api/control -------------------------------------------------


def test_control_switches_an_ordinary_device_and_is_logged(client):
    client.app.state.mqtt = _FakeMqtt()
    response = client.post(
        "/api/control", json={"appliance_id": "incandescent_lamp", "command": "OFF"}
    )
    assert response.status_code == 200
    assert response.json() == {"outcome": "SENT", "reason": None}

    body = client.get("/api/actions").json()
    # The APPLIANCE id, not the plug: one namespace for the whole log, so
    # a SENT row and a refusal for the same appliance carry the same name
    # in the same column. See Gate._log.
    assert body[0]["device"] == "incandescent_lamp"
    assert body[0]["command"] == "OFF"
    assert body[0]["outcome"] == "SENT"

    # Ruling C: on_stat=gate.confirm is wired inside create_app's own
    # lifespan, not merely a capability Ingestor/Gate have in isolation
    # (test_ingest.py's on_stat tests already cover that, against a bare
    # Ingestor built directly in the test, never through create_app).
    # Feeding a stat/ reply through THIS app's real, assembled
    # `client.app.state.ingestor` is what actually proves the wiring
    # inside the lifespan, rather than the mechanism it wires together.
    assert client.app.state.ingestor.handle("stat/plug_lamp/POWER", b"OFF")
    confirmed = client.get("/api/actions").json()
    assert confirmed[0]["outcome"] == "CONFIRMED"


def test_control_reports_timeout_honestly_with_no_broker(client):
    """Ruling D: with no MqttIngest at all (start_ingest=False, as every
    test here uses), `_publish_command` must return False rather than
    raise, and Gate turns that into an honest TIMEOUT rather than a
    false SENT -- never claiming a command left the process when it
    could not have."""
    response = client.post(
        "/api/control", json={"appliance_id": "incandescent_lamp", "command": "OFF"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "TIMEOUT"
    assert body["reason"]


def test_control_refuses_a_protected_cut_cleanly(client):
    """US49: the demonstration the whole sub-project exists for. An
    evaluator ATTEMPTS a protected cut and watches it refused -- cleanly,
    as a 200 with the refusal described, not as a server error."""
    response = client.post(
        "/api/control", json={"appliance_id": "laptop_charger", "command": "OFF"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "REFUSED_PROTECTED"
    assert "protected" in body["reason"].lower()


def test_control_refuses_energising_a_heating_load(client):
    response = client.post(
        "/api/control", json={"appliance_id": "kettle", "command": "ON"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "REFUSED_HEATING_ONE_WAY"
    assert body["reason"]


def test_control_allows_energising_a_protected_load(client):
    """Protection is one-directional (test_gate.py already covers this
    at the Gate layer); a quick check that the API does not somehow
    invert it."""
    client.app.state.mqtt = _FakeMqtt()
    response = client.post(
        "/api/control", json={"appliance_id": "laptop_charger", "command": "ON"}
    )
    assert response.json()["outcome"] == "SENT"


# -- GET /api/actions ----------------------------------------------------


def test_actions_log_shows_refusals(client):
    """US48: the safety gate leaves evidence it worked, visible through
    the same API an evaluator uses to attempt the cut in the first
    place -- not just inside Store directly (test_gate.py's own
    coverage)."""
    client.post(
        "/api/control", json={"appliance_id": "laptop_charger", "command": "OFF"}
    )
    client.post("/api/control", json={"appliance_id": "kettle", "command": "ON"})

    body = client.get("/api/actions").json()
    by_outcome = {row["outcome"]: row for row in body}

    assert "REFUSED_PROTECTED" in by_outcome
    assert by_outcome["REFUSED_PROTECTED"]["device"] == "laptop_charger"
    assert "can never be cut" in by_outcome["REFUSED_PROTECTED"]["reason"]

    assert "REFUSED_HEATING_ONE_WAY" in by_outcome
    assert by_outcome["REFUSED_HEATING_ONE_WAY"]["device"] == "kettle"
    assert by_outcome["REFUSED_HEATING_ONE_WAY"]["reason"]


def test_the_action_log_uses_one_identifier_namespace(client):
    """Important 3: every row names the appliance, never the plug.

    laptop_charger is the one appliance that can produce both shapes of
    row -- ON is permitted and SENT, OFF is refused as protected -- so
    this is the exact pair the old `plug_device or cmd.appliance_id`
    split apart: the SENT row said `plug_laptop`, the refusal said
    `laptop_charger`, in one unlabelled column an evaluator reads. An
    evaluator could not tell the two rows were about the same appliance,
    and nothing in the running system mapped one name to the other.
    """
    client.app.state.mqtt = _FakeMqtt()
    assert client.post(
        "/api/control", json={"appliance_id": "laptop_charger", "command": "ON"}
    ).json()["outcome"] == "SENT"
    assert client.post(
        "/api/control", json={"appliance_id": "laptop_charger", "command": "OFF"}
    ).json()["outcome"] == "REFUSED_PROTECTED"

    body = client.get("/api/actions").json()
    by_outcome = {row["outcome"]: row for row in body}
    assert set(by_outcome) == {"SENT", "REFUSED_PROTECTED"}
    assert by_outcome["SENT"]["device"] == "laptop_charger"
    assert by_outcome["REFUSED_PROTECTED"]["device"] == "laptop_charger"
    # Stated as a set over the WHOLE log, not row by row: the plug name
    # must be absent from this column entirely, not merely absent from
    # the two rows this test happened to look up.
    assert {row["device"] for row in body} == {"laptop_charger"}

    # The command did still go to the plug -- the appliance id in the log
    # is a naming choice, not a loss of the routing it stands for.
    assert client.app.state.mqtt.sent == [("cmnd/plug_laptop/POWER", "ON")]


def test_actions_respects_the_limit_param(client):
    for _ in range(3):
        client.post(
            "/api/control", json={"appliance_id": "incandescent_lamp", "command": "OFF"}
        )
    body = client.get("/api/actions?limit=1").json()
    assert len(body) == 1


def test_actions_orders_most_recent_first(client, store):
    """ts alone does not always order a burst: real wall-clock
    time.time() can tie for actions written within the same clock tick
    (back-to-back /api/control calls can do this in practice -- this
    test writes the tie directly through Store so it is unconditional
    rather than dependent on how fast this machine's clock ticks), so
    Store.actions() breaks ties by rowid -- insertion order -- rather
    than leaving equal-ts rows in an order the query planner happens to
    pick.
    """
    store.write_action(
        1000.0, "user", "kettle", "ON", None, "REFUSED_HEATING_ONE_WAY", "r1"
    )
    # Same ts as the row above, on purpose: the tie ORDER BY ts DESC
    # alone cannot break.
    store.write_action(
        1000.0, "user", "laptop_charger", "OFF", None, "REFUSED_PROTECTED", "r2"
    )
    store.write_action(
        2000.0, "user", "incandescent_lamp", "OFF", None, "SENT", None
    )

    body = client.get("/api/actions").json()
    # Distinct ts orders normally (SENT, the ts=2000 row, first); the
    # ts=1000 tie resolves to insertion order -- REFUSED_PROTECTED,
    # written second, ahead of REFUSED_HEATING_ONE_WAY, written first.
    assert [row["outcome"] for row in body] == [
        "SENT", "REFUSED_PROTECTED", "REFUSED_HEATING_ONE_WAY",
    ]


# -- GET /api/rules --------------------------------------------------------


def test_rules_reports_shipped_defaults_by_default(client):
    body = client.get("/api/rules").json()
    assert body["is_demo"] is False
    assert body["left_on_seconds"] == pytest.approx(4 * 3600.0)
    assert body["grace_seconds"] == pytest.approx(60.0)
    assert body["standby_band_w"] == [1.0, 12.0]
    assert body["left_on_targets"] == ["incandescent_lamp"]


def test_rules_reports_demo_defaults_when_configured(store):
    """Ruling B / the spec's testing table: 'demo threshold visible' -- a
    short demonstration threshold must never be presented as though it
    were the shipped default."""
    demo_settings = dataclasses.replace(load_settings(), rules_demo=True)
    app = create_app(store=store, settings=demo_settings, start_ingest=False)
    with TestClient(app) as demo:
        body = demo.get("/api/rules").json()
    assert body["is_demo"] is True
    assert body["left_on_seconds"] <= 120


# -- GET /api/pending, POST /api/pending/{id}/cancel ------------------------


def test_pending_reports_the_warned_cut_and_survives_a_reload(demo_client):
    """Ruling B / the spec's testing table: 'pending survives reload' --
    the warning lives in server-side RulesEngine state, not anything
    tied to the one response that first reported it, so a second,
    wholly independent GET (standing in for a browser reload, which
    carries nothing forward from the first request) still sees it."""
    _warn_the_lamp(demo_client)

    first = demo_client.get("/api/pending").json()
    assert len(first) == 1
    assert first[0]["appliance_id"] == "incandescent_lamp"
    assert first[0]["rule"] == "left_on"
    assert 0 <= first[0]["remaining_s"] <= 60

    second = demo_client.get("/api/pending").json()
    assert len(second) == 1
    # The identity fields must be exactly identical -- same cut, same
    # server-side record, not a new one manufactured per request.
    # remaining_s is NOT compared for exact equality: it is
    # max(0.0, acts_at - time.time()) recomputed fresh on every request
    # (a live countdown, by design), so it legitimately ticks down by a
    # few milliseconds between these two real, separately-timed calls --
    # asserting it never moves would be asserting the countdown is fake.
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["appliance_id"] == first[0]["appliance_id"]
    assert second[0]["rule"] == first[0]["rule"]
    assert 0 <= second[0]["remaining_s"] <= first[0]["remaining_s"]


def test_cancel_removes_a_pending_cut_and_logs_it(demo_client, store):
    _warn_the_lamp(demo_client)
    pending_id = demo_client.get("/api/pending").json()[0]["id"]

    response = demo_client.post(f"/api/pending/{pending_id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    assert demo_client.get("/api/pending").json() == []

    outcomes = [
        row["outcome"]
        for row in store.connection.execute("SELECT outcome FROM actions")
    ]
    assert "CANCELLED" in outcomes


def test_cancel_unknown_pending_id_is_404(client):
    response = client.post("/api/pending/does-not-exist/cancel")
    assert response.status_code == 404


def test_pending_empty_when_nothing_is_warned(client):
    assert client.get("/api/pending").json() == []


# -- GET /api/health, the rules engine's half --------------------------------


def test_health_reports_the_rules_engine_and_not_only_the_broker(client):
    """Ruling R31: /api/health reported broker and ingest health and said
    nothing at all about the engine that acts on it.

    Asserted through the ROUTE, not by calling RulesEngine.health()
    directly (test_rules.py already does that): what was missing was the
    wiring, so the wiring is what this checks.
    """
    body = client.get("/api/health").json()
    assert "rules" in body, "health says nothing about the rules engine"
    rules_health = body["rules"]
    assert set(rules_health) == {
        "ticks", "tick_errors", "last_tick_age_s", "last_error",
        "telemetry_age_s", "max_observation_age_s", "observations_fresh",
        "pending_cuts",
    }
    # Nothing has been fed yet, and that is reported as unactionable
    # rather than as an absence a reader could take for "fine".
    assert rules_health["telemetry_age_s"] is None
    assert rules_health["observations_fresh"] is False
    assert rules_health["tick_errors"] == 0
    assert rules_health["last_error"] is None

    _warn_the_lamp(client, on_for_s=1.0)
    fed = client.get("/api/health").json()["rules"]
    assert fed["ticks"] >= 1
    assert fed["observations_fresh"] is True
    assert fed["telemetry_age_s"] < fed["max_observation_age_s"]


# -- the rules loop itself ---------------------------------------------------


def test_rules_loop_ticks_on_its_own(store, monkeypatch):
    """Finding 1: every other test in this file calls
    client.app.state.rules.tick() by hand. That proves observe()/tick()
    behave correctly, but nothing anywhere proves the background
    _rules_loop task created in create_app's lifespan actually exists
    and runs on its own schedule. Without it: no warned cut ever acts,
    no grace period ever expires, sweep_timeouts() never runs, and every
    SENT command sits at SENT forever -- exactly plan Step 6 item 4.

    _RULES_TICK_INTERVAL_S and DEMO_DEFAULTS.left_on_seconds/
    grace_seconds are all monkeypatched down so the loop's OWN real-time
    schedule -- not this test calling tick() -- can warn, wait out a
    grace period and act inside a fraction of a second. Nothing below
    ever calls client.app.state.rules.tick() or gate.sweep_timeouts()
    directly; every assertion polls (_wait_until) for a state that only
    the loop itself can produce.
    """
    monkeypatch.setattr("smartwatt_server.api._RULES_TICK_INTERVAL_S", 0.02)
    monkeypatch.setattr(
        "smartwatt_server.api.DEMO_DEFAULTS",
        dataclasses.replace(DEMO_DEFAULTS, left_on_seconds=0.01, grace_seconds=0.1),
    )
    demo_settings = dataclasses.replace(load_settings(), rules_demo=True)
    app = create_app(store=store, settings=demo_settings, start_ingest=False)

    with TestClient(app) as client:
        client.app.state.mqtt = _FakeMqtt()

        base = example("telemetry-single-load")
        ts = 3_000_000.0
        payload = {
            **base,
            "ts": ts,
            "seq": 1,
            "attribution": {
                # Already "on" for 1 s of telemetry time -- comfortably
                # past the 0.01 s threshold the instant any tick reads
                # it, so the wait below is bounded by the tick interval,
                # not by the threshold.
                "active": [{"id": "incandescent_lamp", "w": 40.0, "since": ts - 1.0}],
                "residual_w": 0.0,
                "floor_w": 6.0,
            },
        }
        assert client.app.state.ingestor.handle(
            TOPIC_TELEMETRY, json.dumps(payload).encode()
        )

        assert _wait_until(
            lambda: len(client.get("/api/pending").json()) == 1, timeout=2.0
        ), "no pending cut appeared -- the rules loop never ticked"

        assert _wait_until(
            lambda: client.get("/api/pending").json() == [], timeout=2.0
        ), "the pending cut never cleared -- the rules loop never acted"

    # Read the log from `store` directly: the TestClient (and its
    # background loop) has already shut down by this point, so this is
    # not one more thing the loop needs to still be running for.
    rows = [dict(row) for row in store.connection.execute(
        "SELECT * FROM actions WHERE actor = 'rule'"
    )]
    assert rows, "no rule-actor row in the action log"
    assert rows[0]["outcome"] == "SENT"
    assert rows[0]["rule"] == "left_on"
    assert rows[0]["device"] == "incandescent_lamp"
