"""Convert S1's analytic sidecars into a C++ header.

The firmware's tests need the closed-form answers, but pulling a JSON
parser into the firmware to read them would be a dependency in the one
place this project works hardest to keep clean. A generated header costs
nothing and puts the expected values in the compiler's view.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "test" / "fixtures"

HEADER = """// GENERATED FILE - do not edit by hand.
// Regenerate with: uv run python firmware/tools/make_expected.py
// Source: firmware/test/fixtures/*.sidecar.json

#pragma once

struct ExpectedFixture {
  const char* name;
  double v_rms;
  double freq;
  double p;
  double q1;
  double dist;
  double s;
  double irms;
  double pf_true;
  double pf_disp;
  double crest;
  double dist_from_spectrum;  // -1.0 when the pure-V identity does not hold
  // Harmonic h RMS current over the fundamental's, from the sidecar's
  // harmonic_rms map. 0.0 when the fixture carries no such harmonic, or no
  // usable fundamental to divide by -- `idle` has neither.
  double h3_ratio;
  double h5_ratio;
  double h7_ratio;
  bool cross_derivation_holds;
};

"""


def harmonic_ratio(harmonics: dict, h: int) -> float:
    """Harmonic `h` over the fundamental, or 0.0 when either is unusable."""
    fundamental = harmonics.get("1")
    if not fundamental:
        return 0.0
    return harmonics.get(str(h), 0.0) / fundamental


def main() -> None:
    rows = []
    for path in sorted(FIXTURES.glob("*.sidecar.json")):
        s = json.loads(path.read_text())
        spectrum = s["dist_from_spectrum"]
        harmonics = s.get("harmonic_rms") or {}
        rows.append(
            "  {{\"{name}\", {v_rms!r}, {freq!r}, {p!r}, {q1!r}, {dist!r}, "
            "{s!r}, {irms!r}, {pf_true!r}, {pf_disp!r}, {crest!r}, "
            "{spectrum!r}, {h3!r}, {h5!r}, {h7!r}, {holds}}}".format(
                name=s["name"],
                v_rms=s["v_rms"], freq=s["freq"], p=s["p"], q1=s["q1"],
                dist=s["dist"], s=s["s"], irms=s["irms"],
                pf_true=s["pf_true"], pf_disp=s["pf_disp"], crest=s["crest"],
                spectrum=(-1.0 if spectrum is None else spectrum),
                h3=harmonic_ratio(harmonics, 3),
                h5=harmonic_ratio(harmonics, 5),
                h7=harmonic_ratio(harmonics, 7),
                holds="true" if s["cross_derivation_holds"] else "false",
            )
        )

    body = (
        HEADER
        + "static const ExpectedFixture EXPECTED[] = {\n"
        + ",\n".join(rows)
        + "\n};\n\n"
        + f"static const int EXPECTED_COUNT = {len(rows)};\n"
    )
    out = FIXTURES / "expected.h"
    out.write_text(body, encoding="utf-8")
    print(f"wrote {out} with {len(rows)} fixtures")


if __name__ == "__main__":
    main()
