import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from smartwatt_server.config import settings
from smartwatt_server.ingest import (
    TOPIC_EVENT,
    TOPIC_TELEMETRY,
    CommandTopicRefused,
    Ingestor,
    MqttIngest,
)
from smartwatt_server.ledger import LedgerIntegrator

from .support import example


@pytest.fixture
def ingestor(store):
    return Ingestor(store, LedgerIntegrator(store))


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def test_valid_telemetry_is_accepted(store, ingestor):
    assert ingestor.handle(TOPIC_TELEMETRY, _body(example("telemetry-single-load")))
    assert store.latest_telemetry() is not None
    assert ingestor.accepted == 1


def test_valid_event_is_accepted(store, ingestor):
    assert ingestor.handle(TOPIC_EVENT, _body(example("event-on-confident")))
    assert len(store.events()) == 1


def test_malformed_json_is_rejected(store, ingestor):
    assert ingestor.handle(TOPIC_TELEMETRY, b"{not json") is False
    assert store.latest_telemetry() is None
    assert ingestor.rejected == 1


def test_schema_violation_rejects_the_whole_message(store, ingestor):
    """Never a partial ingest."""
    payload = example("telemetry-single-load")
    payload["electrical"]["range"] = "medium"
    assert ingestor.handle(TOPIC_TELEMETRY, _body(payload)) is False
    assert store.latest_telemetry() is None
    assert ingestor.rejected == 1


def test_missing_field_rejects(store, ingestor):
    payload = example("telemetry-single-load")
    del payload["attribution"]["floor_w"]
    assert ingestor.handle(TOPIC_TELEMETRY, _body(payload)) is False
    assert store.latest_telemetry() is None


def test_rejected_count_is_stored(store, ingestor):
    ingestor.handle(TOPIC_TELEMETRY, b"{bad")
    assert store.stats()["rejected_schema"] == 1


def test_seq_gap_is_counted(store, ingestor):
    first = example("telemetry-single-load")
    second = {**first, "ts": first["ts"] + 1, "seq": first["seq"] + 5}
    ingestor.handle(TOPIC_TELEMETRY, _body(first))
    ingestor.handle(TOPIC_TELEMETRY, _body(second))
    assert ingestor.seq_gaps == 1
    assert store.stats()["seq_gaps"] == 1


def test_seq_gap_does_not_interpolate(store, ingestor):
    """A gap is a fact about the run. Inventing samples to smooth a chart
    is exactly what non-negotiable #2 forbids."""
    first = example("telemetry-single-load")
    second = {**first, "ts": first["ts"] + 1, "seq": first["seq"] + 5}
    ingestor.handle(TOPIC_TELEMETRY, _body(first))
    ingestor.handle(TOPIC_TELEMETRY, _body(second))
    assert len(store.telemetry_since(0)) == 2


def test_contiguous_seq_is_not_a_gap(store, ingestor):
    first = example("telemetry-single-load")
    second = {**first, "ts": first["ts"] + 1, "seq": first["seq"] + 1}
    ingestor.handle(TOPIC_TELEMETRY, _body(first))
    ingestor.handle(TOPIC_TELEMETRY, _body(second))
    assert ingestor.seq_gaps == 0


def test_negative_residual_ingests_unclamped(store, ingestor):
    ingestor.handle(TOPIC_TELEMETRY, _body(example("telemetry-negative-residual")))
    assert store.latest_telemetry()["residual_w"] < 0


def test_unknown_topic_is_ignored(store, ingestor):
    assert ingestor.handle("smartwatt/nonsense", _body({"a": 1})) is False
    assert ingestor.rejected == 0


def test_on_payload_hook_fires(store):
    seen = []
    ingestor = Ingestor(store, LedgerIntegrator(store), on_payload=lambda k, p: seen.append(k))
    ingestor.handle(TOPIC_TELEMETRY, _body(example("telemetry-single-load")))
    ingestor.handle(TOPIC_EVENT, _body(example("event-on-confident")))
    assert seen == ["telemetry", "event"]


def test_hook_does_not_fire_on_rejection(store):
    seen = []
    ingestor = Ingestor(store, LedgerIntegrator(store), on_payload=lambda k, p: seen.append(k))
    ingestor.handle(TOPIC_TELEMETRY, b"{bad")
    assert seen == []


# -- stat/ replies: the gate's confirmation path -----------------------------


def test_stat_topic_dispatches_to_on_stat(store):
    seen = []
    ingestor = Ingestor(
        store, LedgerIntegrator(store),
        on_stat=lambda device, body: seen.append((device, body)),
    )
    assert ingestor.handle("stat/plug_lamp/POWER", b"ON") is True
    assert seen == [("plug_lamp", b"ON")]


def test_stat_topic_without_on_stat_configured_is_ignored(store, ingestor):
    # `ingestor` leaves on_stat at its default (None), the same way every
    # other test in this file constructs one without on_payload either.
    assert ingestor.handle("stat/plug_lamp/POWER", b"ON") is False


@pytest.mark.parametrize(
    "topic",
    [
        "stat/POWER",              # missing the device segment entirely
        "stat//POWER",             # empty device segment
        "stat/plug_lamp/power",    # wrong case on the last segment
        "POWER",
        "stat/plug_lamp/POWER/extra",
        "stat/a/b/POWER",
    ],
)
def test_malformed_stat_topics_are_not_dispatched(store, topic):
    """The topic-shape guard, not the callback's own robustness, is what
    must hold here: on_stat below records anything it is ever called
    with, so an empty `seen` proves handle() never even reached it."""
    seen = []
    ingestor = Ingestor(
        store, LedgerIntegrator(store),
        on_stat=lambda device, body: seen.append(device),
    )
    assert ingestor.handle(topic, b"ON") is False
    assert seen == []


def test_stat_topic_does_not_affect_accepted_or_rejected_counters(store, ingestor):
    """stat/ traffic is orthogonal to telemetry/event ingest health --
    /api/health's accepted/rejected_schema counters must not be skewed
    by confirmation replies that were never JSON, let alone schema-
    checked."""
    ingestor.handle("stat/plug_lamp/POWER", b"ON")
    assert ingestor.accepted == 0
    assert ingestor.rejected == 0


def test_on_stat_exception_does_not_escape_handle(store, caplog):
    """A misbehaving on_stat callback must not be able to crash the
    ingest thread: an exception escaping handle() reaches paho and kills
    it outright (see MqttIngest._run()'s own note on that cost), taking
    telemetry and event ingest down along with plug confirmations."""
    def _boom(device, body):
        raise RuntimeError("on_stat blew up")

    ingestor = Ingestor(store, LedgerIntegrator(store), on_stat=_boom)
    with caplog.at_level(logging.ERROR, logger="smartwatt_server.ingest"):
        assert ingestor.handle("stat/plug_lamp/POWER", b"ON") is False
    assert any(
        record.levelno >= logging.ERROR and record.exc_info
        for record in caplog.records
    ), "the failure was not logged with a traceback"


# -- MqttIngest.publish() -----------------------------------------------------


def test_publish_without_a_client_reports_failure(store):
    """No start() call means no thread and no client -- exactly the path
    api.py's `_publish_command` closure takes whenever start_ingest=False
    (every existing API test) or a real broker has not connected yet."""
    mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))
    assert mqtt_ingest.publish("stat/plug_lamp/POWER", "OFF") is False


def test_publish_delegates_to_the_paho_client(store):
    mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))

    class _FakeInfo:
        rc = 0  # MQTT_ERR_SUCCESS

    class _FakeClient:
        def __init__(self):
            self.published = []

        def publish(self, topic, payload, retain=False):
            self.published.append((topic, payload))
            return _FakeInfo()

    fake_client = _FakeClient()
    mqtt_ingest._client = fake_client  # noqa: SLF001 - stands in for _run()'s own assignment
    assert mqtt_ingest.publish("some/topic", "ON") is True
    assert fake_client.published == [("some/topic", "ON")]


def test_publish_reports_failure_when_paho_reports_no_connection(store):
    """Confirms `publish()` trusts paho's own info.rc rather than only
    checking `self._client is None` -- a constructed-but-disconnected
    client (the ordinary state between start() and the first successful
    connect, or after a drop) must also report failure, not raise and
    not claim success."""
    import paho.mqtt.client as mqtt

    mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))

    class _FakeInfo:
        rc = mqtt.MQTT_ERR_NO_CONN

    class _FakeClient:
        def publish(self, topic, payload, retain=False):
            return _FakeInfo()

    mqtt_ingest._client = _FakeClient()  # noqa: SLF001
    assert mqtt_ingest.publish("some/topic", "ON") is False


class _RecordingClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))
        return SimpleNamespace(rc=0)


def test_publish_passes_retain_through_to_paho(store):
    """smartwatt/fingerprints is RETAINED so a rebooted device gets the
    table back; a flag dropped on the way to paho would lose that silently."""
    mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))
    fake_client = _RecordingClient()
    mqtt_ingest._client = fake_client  # noqa: SLF001
    assert mqtt_ingest.publish("some/topic", "x", retain=True) is True
    assert mqtt_ingest.publish("some/topic", "y") is True
    assert mqtt_ingest.publish_retained("some/topic", "z") is True
    assert fake_client.published == [
        ("some/topic", "x", True),
        ("some/topic", "y", False),
        ("some/topic", "z", True),
    ]


def test_a_retained_publish_still_refuses_a_command_topic(store):
    mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))
    fake_client = _RecordingClient()
    mqtt_ingest._client = fake_client  # noqa: SLF001
    with pytest.raises(CommandTopicRefused):
        mqtt_ingest.publish("cmnd/plug_lamp/POWER", "OFF", retain=True)
    with pytest.raises(CommandTopicRefused):
        mqtt_ingest.publish_retained("cmnd/plug_lamp/POWER", "OFF")
    assert fake_client.published == []


def test_a_command_is_never_retained(store):
    """publish_command stays non-retained: a retained OFF would re-cut the
    plug every time it reconnected, long after anyone decided to."""
    authority = object()
    mqtt_ingest = MqttIngest(
        settings(), Ingestor(store, LedgerIntegrator(store)),
        command_authority=authority,
    )
    fake_client = _RecordingClient()
    mqtt_ingest._client = fake_client  # noqa: SLF001
    assert mqtt_ingest.publish_command("cmnd/plug_lamp/POWER", "OFF", authority)
    assert fake_client.published == [("cmnd/plug_lamp/POWER", "OFF", False)]


def test_simulator_and_device_are_indistinguishable(store, ingestor):
    """The whole reason MQTT is retained."""
    device = example("telemetry-single-load")
    simulated = example("telemetry-simulator-null-health")
    assert ingestor.handle(TOPIC_TELEMETRY, _body(device))
    assert ingestor.handle(TOPIC_TELEMETRY, _body(simulated))
    assert ingestor.rejected == 0


def test_mqtt_ingest_against_a_refused_port_stays_disconnected_and_stops(store, monkeypatch):
    """Broker down, at the unit level.

    Port 1 on 127.0.0.1 (an IP literal -- the no-hostname-resolution
    constraint applies to tests too) refuses the connection. Refusal is
    not always sub-millisecond: on this host a loopback RST takes ~2s at
    the OS level (verified independently of paho, with a bare socket), so
    a bare start()-then-stop() races the connect() attempt and usually
    wins before it's ever made, leaving the backoff loop's except-branch
    untested. Waiting on the "unreachable" log record -- rather than
    guessing at a sleep -- costs exactly as long as the real attempt
    takes and guarantees it actually happened.
    """
    monkeypatch.setenv("SMARTWATT_BROKER", "127.0.0.1")
    monkeypatch.setenv("SMARTWATT_BROKER_PORT", "1")
    attempted = threading.Event()

    class _CatchAttempt(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            attempted.set()

    logger = logging.getLogger("smartwatt_server.ingest")
    handler = _CatchAttempt()
    logger.addHandler(handler)
    try:
        mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))
        mqtt_ingest.start()

        assert attempted.wait(timeout=5.0), "connect was never attempted"
        assert mqtt_ingest.connected is False  # can never become True against a refused port

        mqtt_ingest.stop()
        mqtt_ingest._thread.join(timeout=5.0)
        assert not mqtt_ingest._thread.is_alive()
    finally:
        logger.removeHandler(handler)


def test_stop_joins_the_ingest_thread(store, monkeypatch):
    """stop() must not return while the thread might still be writing.

    api.py's lifespan calls stop() and then closes the store's SQLite
    connection. paho's disconnect() only QUEUES a DISCONNECT -- the client
    finishes its current iteration first, which may be inside
    Ingestor.handle -> Store.write_telemetry. Closing the connection under
    that write segfaults CPython, so stop() has to join.

    The stand-in thread below stays busy for 0.3 s after the stop event is
    set, which is exactly the window paho leaves open.
    """
    running = threading.Event()

    def fake_run(self) -> None:
        running.set()
        self._stop.wait(10.0)
        time.sleep(0.3)  # still finishing work when stop() is called

    monkeypatch.setattr(MqttIngest, "_run", fake_run)
    mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))
    mqtt_ingest.start()
    assert running.wait(2.0), "the ingest thread never started"

    mqtt_ingest.stop()
    assert not mqtt_ingest._thread.is_alive(), "stop() returned before the thread ended"


def test_ingest_thread_survives_an_unexpected_error(store, monkeypatch, caplog):
    """paho 2.x re-raises whatever escapes on_message.

    A sqlite error (or a KeyError from a future schema change) inside
    Ingestor.handle therefore propagates straight out of loop_forever. If
    only OSError is caught the thread dies for good, and because nothing
    clears self.connected, /api/health goes on reporting
    broker_connected: true with nothing arriving -- the "stale numbers as
    live" the spec forbids.
    """
    import paho.mqtt.client as mqtt

    attempts = []
    #: self.connected as each connect attempt begins. The retry must see
    #: False: a thread that has just blown up is not a connected broker.
    connected_at_attempt = []
    second_attempt = threading.Event()
    released = threading.Event()

    class _FailingClient:
        def __init__(self) -> None:
            self.on_connect = self.on_disconnect = self.on_message = None

        def connect(self, host, port, keepalive):
            connected_at_attempt.append(mqtt_ingest.connected)
            self.on_connect(self, None, None, 0)

        def subscribe(self, topic):
            pass

        def disconnect(self):
            released.set()

        def loop_forever(self, retry_first_connection=False):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("on_message blew up")
            second_attempt.set()
            released.wait(10.0)

    monkeypatch.setattr(mqtt, "Client", lambda *args, **kwargs: _FailingClient())

    mqtt_ingest = MqttIngest(settings(), Ingestor(store, LedgerIntegrator(store)))
    with caplog.at_level(logging.ERROR, logger="smartwatt_server.ingest"):
        mqtt_ingest.start()
        try:
            assert second_attempt.wait(10.0), "the thread died on the first error"
            # on_connect had set it True; the guard must clear it, or
            # health reports a live broker with nothing arriving.
            assert connected_at_attempt == [False, False]
        finally:
            mqtt_ingest.stop()

    assert any(
        record.levelno >= logging.ERROR and record.exc_info
        for record in caplog.records
    ), "the failure was not logged with a traceback"
