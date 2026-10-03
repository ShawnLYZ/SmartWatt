"""MQTT publishing, recording and replay.

MQTT is what makes the simulator and the device interchangeable publishers:
the server cannot tell them apart, so switching between them is a physical
act with no code path change.
"""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from .scenarios import run

TOPICS = {
    "telemetry": "smartwatt/telemetry",
    "event": "smartwatt/event",
}


def _new_client():
    import paho.mqtt.client as mqtt

    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


def record(scenario_name: str, out: Path, *, start_ts: float = 1754035200.0) -> int:
    """Write a scenario to newline-delimited JSON. Returns lines written."""
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", encoding="utf-8") as handle:
        for kind, payload in run(scenario_name, start_ts=start_ts):
            handle.write(json.dumps({"kind": kind, "payload": payload}) + "\n")
            written += 1
    return written


def iter_recording(path: Path) -> Iterator[tuple[str, dict]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entry = json.loads(line)
                yield entry["kind"], entry["payload"]


def to_replay(payload: dict) -> dict:
    """Mark a payload as coming from a recorded run. Nothing else changes."""
    payload["source"] = "replay"
    return payload


def publish(
    stream: Iterable[tuple[str, dict]],
    broker_ip: str,
    port: int,
    *,
    speed: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    client=None,
) -> int:
    """Publish a payload stream to the broker, pacing telemetry at 1 Hz / speed.

    ``broker_ip`` must be an IP literal; it is validated here, not merely
    documented. Nothing in this system resolves a name, so a venue mDNS
    failure cannot break it.

    Takes ownership of the connection lifecycle: an injected client is
    disconnected on exit just like one created here.
    """
    ipaddress.ip_address(broker_ip)

    owned = client is None
    if owned:
        client = _new_client()

    client.connect(broker_ip, port, 30)
    client.loop_start()

    sent = 0
    try:
        for kind, payload in stream:
            client.publish(TOPICS[kind], json.dumps(payload), qos=0)
            sent += 1
            if kind == "telemetry":
                sleep(1.0 / speed)
    finally:
        client.loop_stop()
        client.disconnect()

    return sent
