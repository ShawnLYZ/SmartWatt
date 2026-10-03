"""`analyse` — run the measurement harness and write the report."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .fingerprints import MalformedFingerprints, read_fingerprints, to_matrix
from .harness import (
    CannotCrossValidate,
    cross_validate,
    derive_threshold,
    live_accuracy,
    permutation_importance_,
)
from .report import Measured, build_report
from .scatter import write_scatter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyse")
    parser.add_argument("--fingerprints", default="fingerprints.csv")
    parser.add_argument("--live", help="a second fingerprints.csv, held out")
    parser.add_argument(
        "--measured",
        help="JSON of {key: value} for the hardware-measured targets, from "
             "docs/hardware/calibration-log.md",
    )
    parser.add_argument("--conditions", default="")
    parser.add_argument("--out", default="analysis/reports")
    args = parser.parse_args(argv)

    measured = []
    if args.measured:
        try:
            payload = json.loads(Path(args.measured).read_text())
        except FileNotFoundError:
            print(f"error: --measured file not found: {args.measured}")
            return 1
        except OSError as error:
            print(f"error: could not read --measured file {args.measured}: {error}")
            return 1
        except json.JSONDecodeError as error:
            print(f"error: --measured file {args.measured} is not valid JSON: {error}")
            return 1

        if not isinstance(payload, dict):
            print(f"error: --measured file {args.measured} must contain a JSON object")
            return 1

        try:
            for key, value in payload.items():
                measured.append(Measured(key, float(value)))
        except (TypeError, ValueError) as error:
            print(f"error: --measured file {args.measured} has a non-numeric value: {error}")
            return 1

    try:
        rows = read_fingerprints(Path(args.fingerprints))
        live_rows = read_fingerprints(Path(args.live)) if args.live else None
    except (OSError, MalformedFingerprints) as error:
        print(f"error: {error}")
        return 1
    if not rows:
        print("no training rows")
        return 1

    X, y = to_matrix(rows)
    try:
        cv = cross_validate(X, y)
    except CannotCrossValidate as error:
        print(f"error: cannot cross-validate: {error}")
        return 1
    threshold = derive_threshold(X, y)
    importance = permutation_importance_(X, y, repeats=5)

    limitations: list[str] = []
    live = None
    if live_rows is not None:
        if not live_rows:
            limitations.append(
                f"The live file {args.live} has no rows, so live held-out "
                "accuracy was not measured."
            )
        else:
            live_X, live_y = to_matrix(live_rows)
            # The identical, firmware-mirroring classifier cross-validation
            # uses, rejection included, against the full table's threshold.
            live = live_accuracy(X, y, live_X, live_y)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_scatter(X, y, out / "scatter.png")

    report = build_report(
        cv, live.accuracy if live else None, importance, threshold, measured,
        args.conditions,
        live_plain_accuracy=live.plain_accuracy if live else None,
        limitations=limitations,
    )
    for note in limitations + [
        f"class {label!r} has a single row: trained on, not scored"
        for label in cv.unscored
    ]:
        print(f"limitation: {note}")
    path = out / f"{date.today().isoformat()}-report.md"
    path.write_text(report, encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
