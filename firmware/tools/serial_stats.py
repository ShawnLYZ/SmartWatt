"""Average the device's once-a-second status line over a saved serial log.

`pio device monitor -f log2file` saves logs/device-monitor-*.log under the
directory it runs from. This finds main.cpp's status line in that log and
prints the mean and spread of every metric, then two numbers the bench runbook
(docs/hardware/bringup-runbook.md) asks for by name:

  * phi1 = atan2(mean Q1, mean P). On a resistive load that is the phase
    error still uncorrected; the new phase_rad for CAL is the one in use
    plus this.
  * the sampler counters and the voltage-clip count off the LAST line,
    which is what an hour's soak is read from.

Usage: uv run python firmware/tools/serial_stats.py LOG [--skip N] [--last N]
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: main.cpp maybe_report()'s status line. test_bench_tools.py renders the
#: firmware's own printf format through this pattern, so a change to either
#: side turns a test red instead of turning this tool silently blind.
LINE = re.compile(
    r"Vrms=\s*(?P<vrms>\S+)\s+Irms=\s*(?P<irms>\S+)\s+P=\s*(?P<p>\S+)\s+"
    r"Q1=\s*(?P<q1>\S+)\s+D=\s*(?P<d>\S+)\s+PF=\s*(?P<pf>\S+)\s+"
    r"f=\s*(?P<f>\S+)\s+range=(?P<range>\w+)\s+overruns=(?P<overruns>\d+)\s+"
    r"worst=(?P<worst>\d+)us\s+vclip=(?P<vclip>\d+)"
)

#: (field, label) for every averaged metric, in the order the line prints them.
METRICS = (
    ("vrms", "Vrms"), ("irms", "Irms"), ("p", "P"), ("q1", "Q1"),
    ("d", "D"), ("pf", "PF"), ("f", "f"),
)


@dataclass(frozen=True, slots=True)
class StatusLine:
    vrms: float
    irms: float
    p: float
    q1: float
    d: float
    pf: float
    f: float
    range: str
    overruns: int
    worst_us: int
    vclip: int


@dataclass(frozen=True, slots=True)
class Summary:
    count: int
    mean: dict[str, float]
    sd: dict[str, float]
    ranges: dict[str, int]
    phi1_rad: float
    last: StatusLine


def parse_lines(lines: Iterable[str]) -> list[StatusLine]:
    rows: list[StatusLine] = []
    for line in lines:
        match = LINE.search(line)
        if match is None:
            continue
        rows.append(StatusLine(
            **{field: float(match[field]) for field, _ in METRICS},
            range=match["range"],
            overruns=int(match["overruns"]),
            worst_us=int(match["worst"]),
            vclip=int(match["vclip"]),
        ))
    return rows


def summarise(rows: list[StatusLine]) -> Summary:
    if not rows:
        raise ValueError("no status lines to summarise")
    mean = {f: statistics.fmean(getattr(r, f) for r in rows) for f, _ in METRICS}
    sd = {f: statistics.pstdev([getattr(r, f) for r in rows]) for f, _ in METRICS}
    ranges: dict[str, int] = {}
    for row in rows:
        ranges[row.range] = ranges.get(row.range, 0) + 1
    return Summary(
        count=len(rows),
        mean=mean,
        sd=sd,
        ranges=ranges,
        phi1_rad=math.atan2(mean["q1"], mean["p"]),
        last=rows[-1],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path)
    parser.add_argument(
        "--skip", type=int, default=0,
        help="status lines to drop from the start, while the load settles",
    )
    parser.add_argument(
        "--last", type=int, default=0,
        help="use only the last N status lines (after --skip)",
    )
    args = parser.parse_args(argv)

    text = args.log.read_text(encoding="utf-8", errors="ignore")
    rows = parse_lines(text.splitlines())[args.skip:]
    if args.last > 0:
        rows = rows[-args.last:]
    if not rows:
        print(f"no status lines in {args.log} -- is the AC adapter connected?")
        return 1

    s = summarise(rows)
    for field, label in METRICS:
        print(f"{label:>4}: mean {s.mean[field]:11.4f}   sd {s.sd[field]:9.4f}")
    print(f"{s.count} lines; range {s.ranges}")
    if len(s.ranges) > 1:
        print("WARNING: the range changed within these lines -- average one "
              "steady load at a time")
    print(
        f"phi1 = atan2(Q1, P) = {s.phi1_rad:+.6f} rad "
        f"({math.degrees(s.phi1_rad):+.3f} deg). On a resistive load: "
        "new phase_rad = the one in use + this."
    )
    print(
        f"last line: overruns={s.last.overruns}  worst={s.last.worst_us}us  "
        f"vclip={s.last.vclip}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
