import json

import pytest

from smartwatt_sim.publisher import (
    TOPICS,
    iter_recording,
    publish,
    record,
    to_replay,
)
from smartwatt_sim.scenarios import run


class FakeClient:
    """Stands in for paho's client. Records what was published where."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.connected = False

    def connect(self, host, port, keepalive):
        self.connected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        self.connected = False


def test_topics_match_the_contract():
    assert TOPICS["telemetry"] == "smartwatt/telemetry"
    assert TOPICS["event"] == "smartwatt/event"


def test_record_writes_one_json_object_per_line(tmp_path):
    out = tmp_path / "single.jsonl"
    written = record("single", out)
    lines = out.read_text().strip().splitlines()
    assert written == len(lines)
    for line in lines:
        entry = json.loads(line)
        assert entry["kind"] in ("telemetry", "event")
        assert "payload" in entry


def test_recording_round_trips(tmp_path):
    out = tmp_path / "demo.jsonl"
    record("demo", out)
    original = list(run("demo"))
    replayed = list(iter_recording(out))
    assert len(replayed) == len(original)
    for (kind_a, pa), (kind_b, pb) in zip(original, replayed):
        assert kind_a == kind_b
        assert pa == pb


def test_replay_rewrites_source_and_nothing_else():
    """US74: a recorded run must be identical apart from ts and source."""
    _, payload = next(iter(run("single")))
    replayed = to_replay(dict(payload))
    assert replayed["source"] == "replay"
    assert {k: v for k, v in replayed.items() if k != "source"} == {
        k: v for k, v in payload.items() if k != "source"
    }


def test_publish_sends_every_payload_to_the_right_topic():
    client = FakeClient()
    sent = publish(run("single"), "127.0.0.1", 1883,
                   client=client, sleep=lambda _: None)
    assert sent == len(client.published)
    assert {topic for topic, _ in client.published} == {
        TOPICS["telemetry"], TOPICS["event"]
    }


def test_publish_emits_valid_json():
    client = FakeClient()
    publish(run("baseline"), "127.0.0.1", 1883,
            client=client, sleep=lambda _: None)
    for _, body in client.published:
        json.loads(body)


def test_publish_connects_and_disconnects():
    client = FakeClient()
    publish(run("baseline"), "127.0.0.1", 1883,
            client=client, sleep=lambda _: None)
    assert client.connected is False


def test_speed_scales_the_sleep():
    slept: list[float] = []
    client = FakeClient()
    publish(run("single"), "127.0.0.1", 1883,
            client=client, speed=10.0, sleep=slept.append)
    assert slept
    assert max(slept) <= 0.11


def test_broker_address_is_an_ip_literal():
    """US72: nothing in the system resolves a hostname."""
    import inspect

    from smartwatt_sim import publisher

    source = inspect.getsource(publisher)
    assert "localhost" not in source
    assert ".local" not in source


def test_hostname_is_refused_before_connecting():
    """US72: a name must be rejected outright, not resolved."""
    client = FakeClient()
    with pytest.raises(ValueError):
        publish(iter(()), "mqtt.example.invalid", 1883, client=client)
    assert client.connected is False
