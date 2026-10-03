"""Render 4 kSPS three-channel sample streams with an analytic sidecar.

The sidecar states the closed-form answer for every quantity the firmware
signal chain will compute. That is the only way to verify a reactive-power
calculation: no real appliance ever reveals the true value.
"""

from __future__ import annotations

import cmath
import json
import math
from collections.abc import Sequence
from pathlib import Path

from .aggregate import dist_from_spectrum, metrics_from_phasors, sum_phasors
from .appliances import Appliance

SAMPLE_RATE_HZ = 4000.0

#: Derived in the S6 spec from datasheet ratios. Kept here so the simulator
#: renders counts the firmware will see verbatim.
CALIBRATION = {
    "v_cal": 0.2006,        # volts per ADC count, mains referred
    "i_cal_low": 0.0040283,  # amps per ADC count, SCT-013-005
    "i_cal_high": 0.016113,  # amps per ADC count, SCT-013-020
    "adc_mid": 2048,
    "adc_max": 4095,
}

#: Third-harmonic content injected into the voltage waveform when
#: ``distorted_v`` is set, as a fraction of the fundamental.
_V_DISTORTION = 0.05


def _clamp(value: float) -> int:
    return int(max(0, min(CALIBRATION["adc_max"], round(value))))


def _distorted_voltage_power(
    v_rms: float, phasors: dict[int, complex]
) -> tuple[float, float, float]:
    """P, S and the true rms voltage under the actual (distorted) spectrum.

    ``metrics_from_phasors`` assumes a pure fundamental voltage, under which
    a harmonic current carries no average real power. Once the voltage
    itself carries a third harmonic, that harmonic voltage acting on the
    same-order current harmonic does carry real power, and the extra
    voltage rms enters S. Q1, pf_disp, I_rms and crest are unaffected: they
    depend only on the fundamental voltage and the current spectrum, never
    on how distorted the voltage is.

    Returns ``(p, s, v_rms_total)``.
    """
    voltage_spectrum = {1: v_rms, 3: _V_DISTORTION * v_rms}
    p = sum(v_h * phasors.get(h, 0j).real for h, v_h in voltage_spectrum.items())
    v_rms_total = math.sqrt(sum(v_h * v_h for v_h in voltage_spectrum.values()))
    irms = math.sqrt(sum(abs(ph) ** 2 for ph in phasors.values()))
    s = v_rms_total * irms
    return p, s, v_rms_total


def render(
    active: Sequence[Appliance],
    v_rms: float,
    freq: float,
    cycles: int,
    *,
    distorted_v: bool = False,
) -> tuple[list[tuple[int, int, int]], dict]:
    """Render ``cycles`` mains periods of three-channel ADC counts.

    Returns ``(samples, sidecar)``. Each sample is ``(v, i_low, i_high)``.

    When ``distorted_v`` is set the voltage carries third-harmonic content,
    which breaks the pure-V identity used for the D cross-derivation. That
    fixture family exists to prove the signal chain computes D from the
    general formula rather than having quietly hardcoded the shortcut.
    """
    phasors = sum_phasors(active)
    metrics = metrics_from_phasors(v_rms, phasors)

    total = int(round(SAMPLE_RATE_HZ / freq * cycles))
    dt = 1.0 / SAMPLE_RATE_HZ
    v_peak = v_rms * math.sqrt(2.0)

    samples: list[tuple[int, int, int]] = []
    for n in range(total):
        t = n * dt
        omega_t = 2.0 * math.pi * freq * t

        v = v_peak * math.sin(omega_t)
        if distorted_v:
            v += v_peak * _V_DISTORTION * math.sin(3.0 * omega_t)

        i = 0.0
        for order, phasor in phasors.items():
            amplitude = abs(phasor) * math.sqrt(2.0)
            i += amplitude * math.sin(order * omega_t + cmath.phase(phasor))

        samples.append((
            _clamp(CALIBRATION["adc_mid"] + v / CALIBRATION["v_cal"]),
            _clamp(CALIBRATION["adc_mid"] + i / CALIBRATION["i_cal_low"]),
            _clamp(CALIBRATION["adc_mid"] + i / CALIBRATION["i_cal_high"]),
        ))

    spectrum_dist = None if distorted_v else dist_from_spectrum(v_rms, phasors)

    if distorted_v:
        # The pure-V shortcut in `metrics` is wrong here by construction:
        # re-derive P, S, D and pf_true from the actual voltage spectrum.
        # Q1, pf_disp, irms and crest do not depend on voltage distortion.
        p, s, v_rms_total = _distorted_voltage_power(v_rms, phasors)
        dist = math.sqrt(max(0.0, s * s - p * p - metrics.q1 * metrics.q1))
        pf_true = (p / s) if s > 0.0 else 1.0
    else:
        p, s, dist, pf_true = metrics.p, metrics.s, metrics.dist, metrics.pf_true
        v_rms_total = v_rms

    sidecar = {
        "active": [a.id for a in active],
        "v_rms": v_rms,
        "v_rms_total": v_rms_total,
        "freq": freq,
        "cycles": cycles,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "distorted_v": distorted_v,
        "p": p,
        "q1": metrics.q1,
        "dist": dist,
        "s": s,
        "irms": metrics.irms,
        "pf_true": pf_true,
        "pf_disp": metrics.pf_disp,
        "crest": metrics.crest,
        "harmonic_rms": {str(order): abs(ph) for order, ph in sorted(phasors.items())},
        "dist_from_spectrum": spectrum_dist,
        "cross_derivation_holds": not distorted_v,
        "calibration": dict(CALIBRATION),
    }
    return samples, sidecar


def write_fixture(
    name: str,
    active: Sequence[Appliance],
    out_dir: Path,
    *,
    v_rms: float = 240.0,
    freq: float = 50.0,
    cycles: int = 8,
    distorted_v: bool = False,
) -> tuple[Path, Path]:
    """Write ``<name>.csv`` and ``<name>.sidecar.json`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    samples, sidecar = render(
        active, v_rms, freq, cycles, distorted_v=distorted_v
    )
    sidecar["name"] = name

    csv_path = out_dir / f"{name}.csv"
    lines = ["v,i_low,i_high"]
    lines.extend(f"{v},{i_low},{i_high}" for v, i_low, i_high in samples)
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    sidecar_path = out_dir / f"{name}.sidecar.json"
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8", newline="\n")

    return csv_path, sidecar_path
