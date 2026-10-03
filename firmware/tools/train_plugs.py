"""Train the device's classifier by switching real loads through Tasmota plugs.

A bench stand-in for S8's wizard (docs/superpowers/specs/2026-08-13-s8-training-
analysis-design.md), covering its steps 1-3 and 5 without the Setup screen:

  baseline  everything off: the quiet circuit's P level and cycle-to-cycle noise
  capture   per appliance, quiet then overlapped: ON/OFF edges -> fingerprints.csv
  verify    the device must name the appliance correctly three times in a row

Step 4 (the push) is `pio run -e esp32-s3 -t uploadfs` from firmware/, which writes
firmware/data/ to the device's LittleFS, where main.cpp loads
/littlefs/fingerprints.csv at boot. S8's retained-topic push is still unbuilt.

    uv run python firmware/tools/train_plugs.py baseline --plug desk_fan=192.168.137.101 --plug led_bulb=192.168.137.102
    uv run python firmware/tools/train_plugs.py capture  --plug ... --cycles 5
    uv run python firmware/tools/train_plugs.py verify   --plug ...

Each --plug is an appliance id from appliances.toml, "=", and the IP address of
the Tasmota plug it is plugged into (the plug's web page shows it).

SAFETY. This switches plugs directly over Tasmota's HTTP API, not through the
server's gate. So it asks the server's registry first. It refuses to run if any
named appliance is protected or heating, or if a plug's MQTT topic belongs to a
protected or heating appliance. That keeps non-negotiable #3 true for this path as
well: a protected load is never cut by anything, and a heating load is never
energised by anything.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: firmware/lib/features/features.cpp kFeatureNames, in order. The loader reads the
#: fourteen columns after `edge` positionally, so this order IS the file format.
#: test_bench_tools.py pins it to the C++ source.
FEATURE_NAMES = (
    "delta_p", "delta_q1", "delta_dist", "delta_s",
    "pf_disp", "pf_true", "delta_irms", "delta_crest",
    "h3_h1", "h5_h1", "h7_h1", "inrush_ratio",
    "settle_cycles", "log_delta_p",
)
#: The S8 spec's seven capture-condition columns (US59). The loader ignores them.
CONDITION_NAMES = (
    "ts", "session_id", "background_w", "concurrent_ids", "vrms_mean", "freq_mean", "notes",
)
HEADER = ("training_id", "label", "edge") + FEATURE_NAMES + CONDITION_NAMES

#: FingerprintRow's char arrays: label[24] and training_id[32], NUL included.
LABEL_MAX = 23
TRAINING_ID_MAX = 31

#: Event `reason`s whose features describe a real, settled edge. `distance_threshold`
#: is the classifier rejecting an untrained load, which is expected before training.
#: below_floor, no_settle and overlapping_edges mean the features are not a clean
#: step, so a capture that produced one is a failed capture, not a training row.
USABLE_REASONS = (None, "distance_threshold")

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "fingerprints.csv"


def label_problem(label: str) -> str | None:
    """Why the device's loader would drop this label, or None if it would keep it.

    Mirrors is_emittable_label() plus the length test in FileFingerprintSource::load:
    `^[a-z][a-z0-9_]*$`, shorter than 24 bytes, and never `unknown_<digits>`. The
    loader keeps every other `unknown_`-prefixed label.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", label):
        return "must match ^[a-z][a-z0-9_]*$"
    if len(label) > LABEL_MAX:
        return f"longer than {LABEL_MAX} characters"
    if re.fullmatch(r"unknown_[0-9]+", label):
        return "unknown_<n> is reserved for loads the tracker has not named"
    return None


def event_problem(event: dict, expected_edge: str) -> str | None:
    """Why this event cannot be a training row for `expected_edge`, or None."""
    if event.get("edge") != expected_edge:
        return f"edge {event.get('edge')!r}, expected {expected_edge!r}"
    if event.get("reason") not in USABLE_REASONS:
        return f"reason {event.get('reason')!r}"
    features = event.get("features") or {}
    missing = [n for n in FEATURE_NAMES
               if not isinstance(features.get(n), (int, float))
               or not math.isfinite(features[n])]
    if missing:
        # The loader drops a row with a non-finite field, so it is no row at all.
        return f"features missing or non-finite: {', '.join(missing)}"
    return None


def csv_row(event: dict, training_id: str, label: str, conditions: dict) -> list[str]:
    """One fingerprints.csv line, in HEADER order, from a device event."""
    if len(training_id) > TRAINING_ID_MAX or "," in training_id:
        raise ValueError(f"training_id {training_id!r} would not survive the loader")
    problem = label_problem(label)
    if problem:
        raise ValueError(f"label {label!r}: {problem}")
    features = event["features"]
    row = [training_id, label, event["edge"]]
    row += [repr(float(features[name])) for name in FEATURE_NAMES]
    for name in CONDITION_NAMES:
        value = conditions.get(name, "")
        text = value if isinstance(value, str) else repr(value)
        if "," in text or "\n" in text:
            raise ValueError(f"condition {name}={text!r} contains a separator")
        row.append(text)
    return row


# -- the bench: plugs, registry, broker ---------------------------------------


def tasmota(ip: str, command: str, timeout: float = 5.0) -> dict:
    url = f"http://{ip}/cm?" + urllib.parse.urlencode({"cmnd": command})
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


@dataclass(frozen=True)
class Plug:
    label: str
    ip: str
    topic: str

    def power(self, state: str) -> None:
        reply = tasmota(self.ip, f"Power {state}")
        if reply.get("POWER") != state:
            raise RuntimeError(f"{self.label} at {self.ip} answered {reply} to Power {state}")


def check_safety(plugs: list[Plug], server: str) -> None:
    """Refuse protected or heating loads: see SAFETY in the module docstring."""
    with urllib.request.urlopen(f"{server}/api/appliances", timeout=5.0) as response:
        appliances = json.loads(response.read())
    by_id = {a["id"]: a for a in appliances}
    guarded = {a["plug_device"]: a["id"] for a in appliances
               if a.get("plug_device") and (a.get("protected") or a.get("heating"))}
    for plug in plugs:
        row = by_id.get(plug.label)
        if row is None:
            raise SystemExit(f"{plug.label} is not in the server's registry, so the dashboard "
                             f"would have no name for it; add it to appliances.toml and "
                             f"restart the server first")
        if row.get("protected") or row.get("heating"):
            raise SystemExit(f"refusing {plug.label}: it is protected or heating")
        if plug.topic in guarded:
            raise SystemExit(f"refusing {plug.ip}: its topic {plug.topic!r} is the plug of "
                             f"{guarded[plug.topic]}, which is protected or heating")


class Broker:
    """The device's events, and its latest telemetry, off the laptop's broker."""

    def __init__(self, host: str, port: int = 1883) -> None:
        import paho.mqtt.client as mqtt

        self.events: queue.Queue[dict] = queue.Queue()
        self.telemetry: list[dict] = []
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"smartwatt-train-{int(time.time())}")
        self._client.on_connect = lambda c, u, f, rc, p: c.subscribe(
            [("smartwatt/event", 0), ("smartwatt/telemetry", 0)])
        self._client.on_message = self._on_message
        self._client.connect(host, port, keepalive=30)
        self._client.loop_start()

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload)
        except ValueError:
            return
        if payload.get("source") != "device":
            return
        if message.topic == "smartwatt/event":
            self.events.put(payload)
        else:
            self.telemetry = (self.telemetry + [payload])[-30:]

    def drain(self) -> list[dict]:
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out

    def wait_events(self, seconds: float) -> list[dict]:
        deadline = time.monotonic() + seconds
        out = []
        while (left := deadline - time.monotonic()) > 0:
            try:
                out.append(self.events.get(timeout=left))
            except queue.Empty:
                break
        return out

    def recent(self, seconds: float) -> list[dict]:
        cutoff = time.time() - seconds
        return [t for t in self.telemetry if t["ts"] >= cutoff]

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def conditions_now(broker: Broker, session_id: str, concurrent: list[str], notes: str) -> dict:
    recent = broker.recent(5.0)
    if not recent:
        raise SystemExit("no device telemetry in the last 5 s: is the board on the hotspot?")
    return {
        "session_id": session_id,
        "background_w": round(statistics.fmean(t["electrical"]["p"] for t in recent), 3),
        "concurrent_ids": ";".join(concurrent),
        "vrms_mean": round(statistics.fmean(t["electrical"]["vrms"] for t in recent), 3),
        "freq_mean": round(statistics.fmean(t["electrical"]["freq"] for t in recent), 4),
        "notes": notes,
    }


def one_edge(broker: Broker, plug: Plug, state: str, window: float) -> tuple[dict | None, str]:
    """Switch `plug` to `state` and return (the one usable event, '') or (None, why)."""
    broker.drain()
    plug.power(state)
    events = broker.wait_events(window)
    edge = state.lower()
    if not events:
        return None, f"no event within {window:.0f} s"
    if len(events) > 1:
        return None, f"{len(events)} events, expected one: " + ", ".join(
            f"{e.get('edge')}/{e.get('reason')}/{e['features'].get('delta_p', 0):.1f} W"
            for e in events)
    problem = event_problem(events[0], edge)
    return (None, problem) if problem else (events[0], "")


# -- commands -----------------------------------------------------------------


def cmd_baseline(plugs: list[Plug], broker: Broker, seconds: float) -> None:
    for plug in plugs:
        plug.power("OFF")
    print(f"all plugs OFF; listening {seconds:.0f} s ...")
    time.sleep(seconds)
    rows = broker.recent(seconds)
    if len(rows) < 5:
        raise SystemExit(f"only {len(rows)} telemetry frames in {seconds:.0f} s")
    p = [t["electrical"]["p"] for t in rows]
    steps = [abs(b - a) for a, b in zip(p, p[1:])]
    events = broker.drain()
    print(f"quiet circuit, {len(rows)} frames: P mean {statistics.fmean(p):.2f} W, "
          f"sd {statistics.pstdev(p):.2f} W, largest 1 s step {max(steps):.2f} W; "
          f"{len(events)} event(s) fired with nothing switching")
    print("the detector opens a candidate at |dP| >= threshold_w (8 W unless main.cpp "
          "sets a measured value); the quiet circuit's noise must sit well under it")


def cmd_capture(plugs: list[Plug], broker: Broker, args) -> None:
    out = Path(args.out)
    if out.exists() and not args.append:
        raise SystemExit(f"{out} exists: pass --append to add rows, or move it away")
    session_id = time.strftime("s%Y%m%d%H%M")
    rows, failures, n = [], [], 0
    for plug in plugs:
        plug.power("OFF")
    time.sleep(args.dwell)
    # Quiet first, then with every other plug's load running (S8 steps 2 and 3).
    phases = [(plug, []) for plug in plugs]
    if len(plugs) > 1:
        phases += [(plug, [o for o in plugs if o is not plug]) for plug in plugs]
    for plug, others in phases:
        mode = "overlap" if others else "quiet"
        for other in others:
            other.power("ON")
        if others:
            time.sleep(args.dwell)
        for cycle in range(args.cycles):
            for state in ("ON", "OFF"):
                for attempt in range(1, args.retries + 1):
                    conditions = conditions_now(broker, session_id,
                                                [o.label for o in others], mode)
                    event, why = one_edge(broker, plug, state, args.window)
                    tag = f"{plug.label:14s} {mode:7s} #{cycle + 1} {state:3s}"
                    if event is None:
                        print(f"  FAILED {tag} (try {attempt}): {why}")
                        failures.append(f"{tag}: {why}")
                        # Put the plug back where the retried edge starts from:
                        # an OFF retried on a plug already OFF has no edge at all.
                        plug.power("OFF" if state == "ON" else "ON")
                        time.sleep(args.dwell)
                        broker.drain()
                        continue
                    n += 1
                    conditions["ts"] = event["ts"]
                    rows.append(csv_row(event, f"{session_id}-{n:03d}", plug.label, conditions))
                    f = event["features"]
                    print(f"  ok     {tag}: dP {f['delta_p']:.1f} W, dQ1 {f['delta_q1']:.1f}, "
                          f"PF {f['pf_true']:.2f}, settle {f['settle_cycles']:.0f} cycles")
                    time.sleep(args.dwell)
                    break
                else:
                    raise SystemExit(f"{plug.label} {state}: {args.retries} tries failed; "
                                     f"nothing written")
        for other in others:
            other.power("OFF")
        time.sleep(args.dwell)
    out.parent.mkdir(parents=True, exist_ok=True)
    fresh = not out.exists()
    with out.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if fresh:
            writer.writerow(HEADER)
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {out} ({len(failures)} failed captures retried)")
    print("next: from firmware/, `pio run -e esp32-s3 -t uploadfs`, then reset the board "
          "and look for 'Fingerprints: loaded' on its serial port")


def cmd_verify(plugs: list[Plug], broker: Broker, args) -> None:
    for plug in plugs:
        plug.power("OFF")
    time.sleep(args.dwell)
    streak, tries = 0, 0
    while streak < 3 and tries < args.max_tries:
        plug = plugs[tries % len(plugs)]
        tries += 1
        event, why = one_edge(broker, plug, "ON", args.window)
        named = event.get("label") if event else None
        correct = event is not None and named == plug.label and not event.get("rejected")
        streak = streak + 1 if correct else 0
        detail = why or f"label {named!r}, confidence {event.get('confidence')}"
        print(f"  {'RIGHT' if correct else 'WRONG'} {plug.label}: {detail}   streak {streak}/3")
        time.sleep(args.dwell)
        plug.power("OFF")
        broker.wait_events(args.window)  # the OFF edge; not scored
        time.sleep(args.dwell)
    print("VERIFIED: three in a row" if streak >= 3 else
          f"NOT VERIFIED: best run ended at streak {streak} after {tries} tries")
    if streak < 3:
        sys.exit(1)


def parse_plugs(specs: list[str]) -> list[Plug]:
    plugs = []
    for spec in specs:
        label, _, ip = spec.partition("=")
        problem = label_problem(label)
        if problem or not ip:
            raise SystemExit(f"--plug {spec!r}: expected label=ip ({problem or 'no ip'})")
        topic = tasmota(ip, "Topic").get("Topic", "")
        plugs.append(Plug(label, ip, topic))
    return plugs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("baseline", "capture", "verify"))
    parser.add_argument("--plug", action="append", required=True, metavar="LABEL=IP")
    parser.add_argument("--broker", default="127.0.0.1")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--cycles", type=int, default=5, help="ON+OFF pairs per phase")
    parser.add_argument("--dwell", type=float, default=8.0, help="seconds between switches")
    parser.add_argument("--window", type=float, default=6.0, help="seconds to wait for an edge")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max-tries", type=int, default=12)
    parser.add_argument("--seconds", type=float, default=60.0, help="baseline length")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args(argv)

    plugs = parse_plugs(args.plug)
    check_safety(plugs, args.server)
    broker = Broker(args.broker)
    try:
        time.sleep(3.0)  # a few telemetry frames before anything is judged
        if args.command == "baseline":
            cmd_baseline(plugs, broker, args.seconds)
        elif args.command == "capture":
            cmd_capture(plugs, broker, args)
        else:
            cmd_verify(plugs, broker, args)
    finally:
        for plug in plugs:
            try:
                plug.power("OFF")
            except Exception as exc:  # leave the bench safe even on a failed run
                print(f"could not switch {plug.label} OFF: {exc}")
        broker.close()


if __name__ == "__main__":
    main()
