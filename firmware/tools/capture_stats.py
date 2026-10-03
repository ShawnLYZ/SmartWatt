"""Summarise a raw capture: bias, peaks, clipping and noise, per channel.

Reads the CSV that capture_to_fixture.py writes. docs/hardware/bringup-runbook.md
uses it for the idle capture (each channel's bias error, the voltage
channel's headroom) and for the 9 W LED milestone (assumption A2: i_low's
peak deviation, which the S6 spec measures from 2048).

Usage: uv run python firmware/tools/capture_stats.py captures/idle.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
from dataclasses import dataclass
from pathlib import Path

#: calibration.h's kAdcMid, kAdcMax and kClipMargin. The last is where the
#: firmware starts treating a sample as clipped, so "CLIPPED" here means
#: exactly what the firmware would decide. test_bench_tools.py reads all
#: three out of calibration.h, so they cannot drift apart.
ADC_MID = 2048
ADC_MAX = 4095
CLIP_MARGIN = 8

SAMPLE_RATE_HZ = 4000
CHANNELS = ("v", "i_low", "i_high")


@dataclass(frozen=True, slots=True)
class ChannelStats:
    name: str
    minimum: int
    maximum: int
    mean: float
    sd: float
    #: The S6 spec's definition of the LED milestone: deviation from 2048.
    peak_from_mid: int
    #: The signal's own amplitude, which differs when the bias is off.
    peak_from_mean: float
    clipped: bool

    @property
    def bias_error(self) -> float:
        return self.mean - ADC_MID


def read_capture(path: Path) -> dict[str, list[int]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{path} holds no samples")
    return {name: [int(row[name]) for row in rows] for name in CHANNELS}


def summarise(name: str, counts: list[int]) -> ChannelStats:
    mean = statistics.fmean(counts)
    return ChannelStats(
        name=name,
        minimum=min(counts),
        maximum=max(counts),
        mean=mean,
        sd=statistics.pstdev(counts),
        peak_from_mid=max(abs(count - ADC_MID) for count in counts),
        peak_from_mean=max(abs(count - mean) for count in counts),
        clipped=min(counts) <= CLIP_MARGIN
        or max(counts) >= ADC_MAX - CLIP_MARGIN,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    args = parser.parse_args(argv)

    channels = read_capture(args.capture)
    count = len(channels["v"])
    print(f"{args.capture}: {count} samples, {count / SAMPLE_RATE_HZ:.2f} s")
    for name in CHANNELS:
        s = summarise(name, channels[name])
        print(
            f"{name:>6}: min {s.minimum:4d}  max {s.maximum:4d}  "
            f"mean {s.mean:7.1f} ({s.bias_error:+6.1f} vs {ADC_MID})  "
            f"sd {s.sd:6.2f}  peak from {ADC_MID} {s.peak_from_mid:4d}  "
            f"peak from mean {s.peak_from_mean:6.1f}"
            + ("  CLIPPED" if s.clipped else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
