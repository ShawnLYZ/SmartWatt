"""Command line entry point.

    sim publish  --scenario demo [--now] [--speed 1.0] [--broker 127.0.0.1] [--port 1883]
    sim record   --scenario demo --out runs/demo.jsonl
    sim replay   runs/demo.jsonl [--speed 1.0]
    sim waveform --fixture pure-resistive --active kettle --out fixtures/
    sim validate <file>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from .appliances import DEFAULT_APPLIANCES_PATH, load_appliances
from .publisher import iter_recording, publish, record, to_replay
from .scenarios import SCENARIOS, run
from .waveform import write_fixture

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contract" / "schemas"


def _validators() -> dict[str, Draft202012Validator]:
    return {
        stem: Draft202012Validator(
            json.loads((SCHEMA_DIR / f"{stem}.schema.json").read_text())
        )
        for stem in ("telemetry", "event")
    }


def _check_scenario(name: str) -> bool:
    if name in SCENARIOS:
        return True
    print(
        f"unknown scenario {name!r}. valid: {', '.join(sorted(SCENARIOS))}",
        file=sys.stderr,
    )
    return False


def _drain(stream, *, dry_run: bool, show: bool, broker: str, port: int, speed: float) -> int:
    if not dry_run:
        return publish(stream, broker, port, speed=speed)
    count = 0
    for _, payload in stream:
        if show:
            print(json.dumps(payload))
        count += 1
    print(f"{count} payloads")
    return 0


def _cmd_publish(args) -> int:
    if not _check_scenario(args.scenario):
        return 2
    # --now starts the scenario's clock at the current time, so the
    # dashboard's "last 15 minutes" chart and this month's totals show the
    # run. Without it every run is stamped from the same fixed date, which
    # keeps recordings and tests reproducible.
    stream = run(args.scenario, start_ts=time.time()) if args.now else run(args.scenario)
    _drain(stream, dry_run=args.dry_run, show=args.print,
           broker=args.broker, port=args.port, speed=args.speed)
    return 0


def _cmd_record(args) -> int:
    if not _check_scenario(args.scenario):
        return 2
    written = record(args.scenario, Path(args.out))
    print(f"wrote {written} payloads to {args.out}")
    return 0


def _cmd_replay(args) -> int:
    source = Path(args.path)
    if not source.exists():
        print(f"no such recording: {source}", file=sys.stderr)
        return 2
    stream = ((kind, to_replay(payload)) for kind, payload in iter_recording(source))
    _drain(stream, dry_run=args.dry_run, show=args.print,
           broker=args.broker, port=args.port, speed=args.speed)
    return 0


def _cmd_waveform(args) -> int:
    catalogue = load_appliances(DEFAULT_APPLIANCES_PATH)
    try:
        active = [catalogue[name] for name in args.active]
    except KeyError as exc:
        print(f"unknown appliance {exc}", file=sys.stderr)
        return 2
    csv_path, sidecar_path = write_fixture(
        args.fixture, active, Path(args.out),
        v_rms=args.vrms, freq=args.freq, cycles=args.cycles,
        distorted_v=args.distorted_v,
    )
    print(f"wrote {csv_path} and {sidecar_path}")
    return 0


def _cmd_validate(args) -> int:
    validators = _validators()
    failures = 0
    for index, (kind, payload) in enumerate(iter_recording(Path(args.path))):
        try:
            validators[kind].validate(payload)
        except ValidationError as exc:
            failures += 1
            print(f"line {index + 1} ({kind}): {exc.message}", file=sys.stderr)
    if failures:
        print(f"{failures} invalid payloads", file=sys.stderr)
        return 1
    print("all payloads valid")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sim", description=__doc__)
    subs = parser.add_subparsers(dest="command")

    def add_transport(sub):
        # An IP literal, never a hostname. US72.
        sub.add_argument("--broker", default="127.0.0.1")
        sub.add_argument("--port", type=int, default=1883)
        sub.add_argument("--speed", type=float, default=1.0)
        sub.add_argument("--dry-run", action="store_true")
        sub.add_argument("--print", action="store_true")

    p_publish = subs.add_parser("publish")
    p_publish.add_argument("--scenario", required=True)
    p_publish.add_argument(
        "--now", action="store_true",
        help="stamp the run from the current time instead of the fixed test date",
    )
    add_transport(p_publish)
    p_publish.set_defaults(func=_cmd_publish)

    p_record = subs.add_parser("record")
    p_record.add_argument("--scenario", required=True)
    p_record.add_argument("--out", required=True)
    p_record.set_defaults(func=_cmd_record)

    p_replay = subs.add_parser("replay")
    p_replay.add_argument("path")
    add_transport(p_replay)
    p_replay.set_defaults(func=_cmd_replay)

    p_wave = subs.add_parser("waveform")
    p_wave.add_argument("--fixture", required=True)
    p_wave.add_argument("--active", nargs="*", default=[])
    p_wave.add_argument("--out", required=True)
    p_wave.add_argument("--vrms", type=float, default=240.0)
    p_wave.add_argument("--freq", type=float, default=50.0)
    p_wave.add_argument("--cycles", type=int, default=8)
    p_wave.add_argument("--distorted-v", action="store_true")
    p_wave.set_defaults(func=_cmd_waveform)

    p_validate = subs.add_parser("validate")
    p_validate.add_argument("path")
    p_validate.set_defaults(func=_cmd_validate)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
