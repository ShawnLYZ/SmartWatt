"""Phasor composition and the power decomposition.

The voltage is modelled as a pure fundamental sinusoid taken as the phase
reference, so V1 is real and positive and harmonic currents carry no average
real power. Under that assumption the distortion residue is derivable two
independent ways, which is what Seam 3 needs.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Iterable
from dataclasses import dataclass

from .appliances import Appliance

#: Samples used to find the peak of a composed current waveform.
_CREST_SAMPLES = 2048


@dataclass(frozen=True, slots=True)
class Metrics:
    p: float
    q1: float
    dist: float
    s: float
    irms: float
    pf_true: float
    pf_disp: float
    crest: float


def sum_phasors(appliances: Iterable[Appliance]) -> dict[int, complex]:
    """Sum harmonic current phasors across parallel loads, per harmonic order.

    A current harmonic lagging the voltage by ``phase`` is the phasor
    ``i_rms * exp(-j * phase)``.
    """
    total: dict[int, complex] = {}
    for appliance in appliances:
        for harmonic in appliance.harmonics:
            contribution = cmath.rect(harmonic.i_rms, -harmonic.phase)
            total[harmonic.h] = total.get(harmonic.h, 0j) + contribution
    return total


def _crest_factor(phasors: dict[int, complex], irms: float) -> float:
    """Peak-to-rms of the composed current waveform.

    Reconstructed on the SINE reference the whole project uses: the
    voltage is v(t) = Vpk*sin(wt), so a harmonic lagging it by phi is
    sqrt(2)*|I_h|*sin(h*wt - phi) = sqrt(2)*|I_h|*sin(h*wt + arg(I_h)).
    waveform.render() renders the same expression, so this analytic
    crest and the crest of the emitted samples agree by construction.
    """
    if irms <= 0.0:
        return 0.0
    peak = 0.0
    for n in range(_CREST_SAMPLES):
        theta = 2.0 * math.pi * n / _CREST_SAMPLES
        value = 0.0
        for order, phasor in phasors.items():
            amplitude = abs(phasor) * math.sqrt(2.0)
            value += amplitude * math.sin(order * theta + cmath.phase(phasor))
        peak = max(peak, abs(value))
    return peak / irms


def metrics_from_phasors(v_rms: float, phasors: dict[int, complex]) -> Metrics:
    """Derive the power decomposition from summed current phasors."""
    i1 = phasors.get(1, 0j)

    # V1 is the real, positive phase reference, so:
    #   P  = Re(V1 * conj(I1)) = v_rms * Re(I1)
    #   Q1 = Im(V1 * conj(I1)) = v_rms * -Im(I1)
    p = v_rms * i1.real
    q1 = v_rms * -i1.imag

    irms = math.sqrt(sum(abs(ph) ** 2 for ph in phasors.values()))
    s = v_rms * irms

    dist = math.sqrt(max(0.0, s * s - p * p - q1 * q1))

    apparent_fundamental = v_rms * abs(i1)
    pf_disp = (p / apparent_fundamental) if apparent_fundamental > 0.0 else 1.0
    pf_true = (p / s) if s > 0.0 else 1.0

    return Metrics(
        p=p, q1=q1, dist=dist, s=s, irms=irms,
        pf_true=pf_true, pf_disp=pf_disp,
        crest=_crest_factor(phasors, irms),
    )


def dist_from_spectrum(v_rms: float, phasors: dict[int, complex]) -> float:
    """The SECOND, INDEPENDENT derivation of the distortion residue.

    With a pure fundamental voltage:

        S^2 - P^2 - Q1^2 = V1^2 * sum_{h>=2} I_h^2

    so D can be obtained from the harmonic spectrum alone, without touching
    S, P or Q1. Agreement between the two routes is what demonstrates that
    reactive power was genuinely separated from distortion rather than
    conflated with it.
    """
    non_fundamental = sum(
        abs(phasor) ** 2 for order, phasor in phasors.items() if order != 1
    )
    return v_rms * math.sqrt(non_fundamental)


def aggregate(v_rms: float, appliances: Iterable[Appliance]) -> Metrics:
    return metrics_from_phasors(v_rms, sum_phasors(appliances))
