"""MQTT ingest.

Every inbound message is validated against its schema BEFORE anything else
happens. A failure rejects the whole message; there is no partial ingest.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from jsonschema import ValidationError

from .config import Settings
from .ledger import LedgerIntegrator
from .schemas import validators
from .store import Store

log = logging.getLogger(__name__)

TOPIC_TELEMETRY = "smartwatt/telemetry"
TOPIC_EVENT = "smartwatt/event"
TOPIC_STAT = "stat/+/POWER"

_KIND_BY_TOPIC = {TOPIC_TELEMETRY: "telemetry", TOPIC_EVENT: "event"}

#: Tasmota's outbound command namespace. Everything under it switches a
#: real relay; everything this process publishes elsewhere does not.
#: This module is the second and last place the literal may appear --
#: gate.py BUILDS a command topic, this one REFUSES one, and
#: test_gate_property.py's scan permits it in exactly those two files.
COMMAND_TOPIC_PREFIX = "cmnd/"


class CommandRefused(RuntimeError):
    """This client declined to put a command topic on the wire.

    Deliberately NOT an OSError subclass (PermissionError would have
    been): an unreachable broker is an OSError here, and a refusal to
    publish is not a network condition and must never be mistaken for
    one -- by ``MqttIngest._run``'s own OSError branch or by a reader.
    """


class CommandTopicRefused(CommandRefused):
    """A command topic was handed to the general-purpose publish path."""


class CommandAuthorityRefused(CommandRefused):
    """A command publish was attempted without this client's capability.

    The one refusal that is about WHO is calling rather than about what
    they passed. A caller can choose its own topic and its own file name;
    it cannot choose to be holding an object it was never given.
    """


def is_command_topic(topic: str) -> bool:
    """Whether ``topic`` addresses Tasmota's command namespace."""
    return topic.startswith(COMMAND_TOPIC_PREFIX)


def _stat_device(topic: str) -> str | None:
    """The ``<device>`` in a ``stat/<device>/POWER`` topic, or None.

    TOPIC_STAT above is the wildcard MqttIngest *subscribes* to; a real
    reply arrives on the concrete topic that matched it, with the device
    name in the middle segment. Matched by shape (three non-empty
    segments, "stat" first and "POWER" last) rather than trusted to only
    ever be that one subscription, so a topic that doesn't fit -- an
    empty or missing device segment, extra segments, anything -- is
    simply not a stat/ message and never raises trying to find out.
    """
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "stat" and parts[2] == "POWER" and parts[1]:
        return parts[1]
    return None


#: How long stop() waits for paho's thread to leave its current iteration.
#: Bounded so a wedged network loop cannot hang shutdown for ever; the
#: alternative -- not waiting at all -- risks closing the database under a
#: running write.
_STOP_JOIN_TIMEOUT_S = 5.0


class Ingestor:
    """Validate, store and dispatch one payload at a time."""

    def __init__(
        self,
        store: Store,
        integrator: LedgerIntegrator,
        on_payload: Callable[[str, dict], None] | None = None,
        on_stat: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self._store = store
        self._integrator = integrator
        self._on_payload = on_payload
        self._on_stat = on_stat
        self.last_seq: int | None = None
        self.seq_gaps = 0
        self.rejected = 0
        self.accepted = 0

    def handle(self, topic: str, body: bytes) -> bool:
        """Returns True if the payload was accepted (and, for telemetry
        and event topics, stored)."""
        device = _stat_device(topic)
        if device is not None:
            return self._handle_stat(device, body)

        kind = _KIND_BY_TOPIC.get(topic)
        if kind is None:
            return False

        ts_min = int(time.time() // 60)

        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            self._reject(ts_min, f"malformed JSON on {topic}: {exc}", body)
            return False

        try:
            validators()[kind].validate(payload)
        except ValidationError as exc:
            self._reject(ts_min, f"schema violation on {topic}: {exc.message}", body)
            return False

        if kind == "telemetry":
            self._check_seq(payload["seq"], ts_min)
            self._store.write_telemetry(payload)
            self._integrator.ingest(payload)
        else:
            self._store.write_event(payload)

        self.accepted += 1
        self._store.bump_stats(ts_min, accepted=1)

        if self._on_payload is not None:
            self._on_payload(kind, payload)
        return True

    def _handle_stat(self, device: str, body: bytes) -> bool:
        """A ``stat/<device>/POWER`` reply -- the safety gate's own
        confirmation path (``Gate.confirm``, in production).

        Deliberately skips the JSON+schema pipeline above entirely:
        Tasmota's POWER payload is a bare state, not a JSON envelope with
        a schema of its own, and ``Gate.confirm`` already normalises
        case and decodes bytes itself, so nothing here needs to.

        Still wrapped in its own try/except, unlike the on_payload call
        above: on_payload only ever fires for a payload that has already
        passed schema validation, but on_stat fires for whatever the
        broker delivers on a wildcard subscription (TOPIC_STAT), wholly
        unvalidated -- a lower-trust input surface, even though the
        production callback (Gate.confirm) is independently safe against
        it. One misbehaving reply must not be able to take down every
        other plug's confirmations, and telemetry/event ingest, along
        with it: an exception escaping handle() reaches paho, which
        re-raises it out of loop_forever(). MqttIngest._run()'s own
        except-block catches that, so the thread itself survives -- but
        every kind of ingest is interrupted for a reconnect-with-backoff
        cycle (1 s doubling to 30 s) rather than for one bad stat/ reply
        alone.
        """
        if self._on_stat is None:
            return False
        try:
            self._on_stat(device, body)
        except Exception:
            log.exception(
                "on_stat callback raised for device %s; ignoring", device
            )
            return False
        return True

    def _reject(self, ts_min: int, message: str, body: bytes) -> None:
        self.rejected += 1
        self._store.bump_stats(ts_min, rejected_schema=1)
        log.warning("%s; payload=%r", message, body[:400])

    def _check_seq(self, seq: int, ts_min: int) -> None:
        if self.last_seq is not None and seq != self.last_seq + 1:
            # Counted, never interpolated over.
            self.seq_gaps += 1
            self._store.bump_stats(ts_min, seq_gaps=1)
            log.info("seq gap: %s -> %s", self.last_seq, seq)
        self.last_seq = seq


class MqttIngest:
    """Runs paho's client on its own thread with exponential backoff."""

    def __init__(
        self,
        settings: Settings,
        ingestor: Ingestor,
        command_authority: object | None = None,
    ) -> None:
        self._settings = settings
        self._ingestor = ingestor
        # The ONE object whose bearer may publish a command topic through
        # this client, taken at construction from the gate that made it
        # (api.py's lifespan does the handover). Name-mangled rather than
        # single-underscore: reaching it from outside now takes
        # `_MqttIngest__command_authority`, which nothing writes by
        # accident and no reader mistakes for supported access.
        #
        # None means this client will publish NO command at all, which is
        # the correct default: an MqttIngest nobody authorised is one no
        # gate is wired to.
        self.__command_authority = command_authority
        self._client = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            self._client.disconnect()
        # disconnect() only QUEUES a DISCONNECT: paho finishes its current
        # iteration first, which may be inside Ingestor.handle ->
        # Store.write_telemetry. api.py closes the SQLite connection as
        # soon as this returns, and closing it under a running statement
        # segfaults the interpreter, so wait for the thread to finish.
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT_S)
            if self._thread.is_alive():
                log.warning(
                    "ingest thread still running after %.0fs; not waiting further",
                    _STOP_JOIN_TIMEOUT_S,
                )

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        """Publish one outbound NON-COMMAND payload. Returns whether paho
        accepted it.

        ``retain`` is passed through to paho unchanged. The broker keeps a
        retained message and hands it to every later subscriber, which is
        what lets a rebooted device pick the fingerprint table back up.

        The general-purpose way anything outside this class reaches the
        private client below, and it REFUSES a command topic -- raising
        rather than publishing, before it looks at anything else, whether
        or not a client exists and whether or not the broker is up. This
        is the runtime half of the gate's structural guarantee, and it is
        the half a source scan cannot provide: ``app.state.mqtt`` is a
        public handle on this object, so any module in the process can
        reach this method, build ``cmnd/<plug>/POWER`` however it likes
        -- including by joining segments that no grep for the literal
        would ever see -- and hand it over. That call now fails where it
        stands instead of switching a relay.

        A command topic goes out through ``publish_command`` below and
        nowhere else. Refusing here rather than routing the topic onward
        is the point: the refusal must be a wall, not a redirect.

        ``_client`` stays None until ``_run()``'s own thread constructs
        one, and forever None if ``start()`` was never called
        (start_ingest=False in tests) -- both ordinary states here, not
        errors, so this reports failure rather than raising. Once a
        client exists, paho's own ``publish()`` already reports "not
        connected" through the returned info's ``rc`` (its internal
        ``_send_publish`` returns MQTT_ERR_NO_CONN whenever its socket is
        absent), so this does not duplicate that by also branching on
        ``self.connected`` -- info.rc is the ground truth of what the
        call itself just did, where ``self.connected`` is a flag that
        could in principle be a moment stale.
        """
        if is_command_topic(topic):
            raise CommandTopicRefused(
                f"{topic!r} is a command topic; only the safety gate may "
                "publish one, through MqttIngest.publish_command()"
            )
        return self._publish(topic, payload, retain)

    def publish_retained(self, topic: str, payload: str) -> bool:
        """``publish(topic, payload, retain=True)``, under its own name.

        Exists for api.py's wiring, which may name no MQTT publish method
        but ``publish_command`` (test_gate_property.py's composition-root
        test). It goes through ``publish`` above, so the command-topic
        refusal applies here exactly as it does there.
        """
        return self.publish(topic, payload, retain=True)

    def publish_command(
        self, topic: str, payload: str, authority: object
    ) -> bool:
        """Publish one command topic, for a caller that holds this
        client's capability. THE ONLY METHOD THAT WILL.

        AUTHORITY FIRST, and it is a check on the CALLER, not on the
        call. An earlier version of this method checked only
        ``is_command_topic(topic)``, and the docstring claimed it was
        "held by exactly one caller" -- which described the closure in
        api.py, not this bound method. This method sits on the object at
        the public ``app.state.mqtt``, so it was exactly as reachable as
        ``publish`` had been, and a three-line reproduction cut a
        PROTECTED appliance with no gate check and no `actions` row.
        That comment was the sentence that made this look safe.

        The fix is a capability. ``Gate`` constructs one
        ``CommandAuthority`` (gate.py), keeps it private, presents it on
        every publish, and hands it to api.py's wiring exactly once,
        which passes it to this client's constructor. The comparison is
        ``is`` -- object identity. A bypass can pick any topic it likes
        and construct as many ``CommandAuthority()`` instances as it
        likes; none of them is THE object this client was given, so none
        of them publishes.

        What this does not do, said rather than left implied: Python has
        no unforgeable references, so a caller determined to reach
        ``_MqttIngest__command_authority`` and pass it back can still get
        through. That is the same irreducible limit the spec's Risks
        section already names, it is a leading underscore louder than
        before, and the architecture test's scan of ``.publish_command``
        references catches the call it must eventually make. What is
        closed is the ordinary route: holding the app is no longer
        enough.

        The topic check stays, second: this must not double as a quieter
        general-purpose publisher for a caller that noticed ``publish``
        had grown a guard.
        """
        if (
            self.__command_authority is None
            or authority is not self.__command_authority
        ):
            raise CommandAuthorityRefused(
                f"a command publish to {topic!r} was attempted without this "
                "client's command authority; only the safety gate holds it"
            )
        if not is_command_topic(topic):
            raise ValueError(
                f"{topic!r} is not a command topic; publish_command() exists "
                "for the safety gate's own topics only -- use publish()"
            )
        return self._publish(topic, payload)

    def _publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        """Hand one already-authorised topic to paho. See ``publish``.

        ``publish_command`` never passes ``retain``: a retained command
        would be replayed to the plug on every reconnect, re-cutting it
        long after anyone decided to.
        """
        if self._client is None:
            return False
        import paho.mqtt.client as mqtt

        info = self._client.publish(topic, payload, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def _run(self) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client = client

        def on_connect(c, userdata, flags, reason_code, properties=None):
            self.connected = reason_code == 0
            for topic in (TOPIC_TELEMETRY, TOPIC_EVENT, TOPIC_STAT):
                c.subscribe(topic)
            log.info("broker connected: %s", self.connected)

        def on_disconnect(c, userdata, flags, reason_code, properties=None):
            self.connected = False
            log.warning("broker disconnected: %s", reason_code)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = lambda c, u, m: self._ingestor.handle(m.topic, m.payload)

        delay = 1.0
        while not self._stop.is_set():
            try:
                client.connect(
                    self._settings.broker_ip, self._settings.broker_port, 30
                )
                delay = 1.0
                client.loop_forever(retry_first_connection=False)
            except Exception as exc:
                if isinstance(exc, OSError):
                    log.warning(
                        "broker unreachable (%s); retrying in %.0fs", exc, delay
                    )
                else:
                    # paho 2.x re-raises whatever escapes on_message, so a
                    # sqlite error or a KeyError inside Ingestor.handle
                    # arrives here. Letting it kill the thread would leave
                    # /api/health reporting a connected broker with nothing
                    # arriving -- stale numbers shown as live.
                    log.exception("ingest thread error; retrying in %.0fs", delay)
                self.connected = False
                self._stop.wait(delay)
                delay = min(delay * 2, 30.0)
