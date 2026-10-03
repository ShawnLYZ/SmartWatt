"""The fourteen-dimensional event signature.

Magnitudes on all deltas, so ON and OFF events share one feature space.
Edge direction is deliberately absent from this module's interface: it is
carried separately and consumed by the tracker, never by the classifier.

Not every dimension is a property of the switched load alone. ``delta_p``,
``delta_q1``, and ``pf_disp`` are background-independent: P and Q1 are
linear in the fundamental current phasor and therefore superpose exactly
across parallel loads, so switching a given appliance yields the same
value regardless of what else is already drawing current. ``delta_dist``,
``delta_s``, ``delta_irms``, ``delta_crest``, and ``pf_true`` are not
background-independent: each is derived from a Euclidean-norm aggregate
over all harmonics rather than a linear sum, and a Euclidean norm does not
superpose, so these five depend on what else was already running at the
moment of the switch as well as on the switched load itself. The effect is
large, not marginal: ``delta_crest`` has been measured swinging by more
than 27x between a quiet and a busy background for the identical switched
load. S5b's classifier and S8's training must account for this: five of
the fourteen dimensions carry information about household context, not
just about the switched appliance.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .aggregate import metrics_from_phasors, sum_phasors
from .appliances import Appliance

FEATURE_NAMES: tuple[str, ...] = (
    "delta_p", "delta_q1", "delta_dist", "delta_s",
    "pf_disp", "pf_true", "delta_irms", "delta_crest",
    "h3_h1", "h5_h1", "h7_h1",
    "inrush_ratio", "settle_cycles", "log_delta_p",
)


def _harmonic_ratio(switched: Appliance, order: int) -> float:
    by_order = {h.h: h.i_rms for h in switched.harmonics}
    fundamental = by_order.get(1, 0.0)
    if fundamental <= 0.0:
        return 0.0
    return by_order.get(order, 0.0) / fundamental


def derive_features(
    v_rms: float,
    before: Sequence[Appliance],
    after: Sequence[Appliance],
    switched: Appliance,
) -> dict[str, float]:
    """Derive the fourteen features from the phasor delta across a switch.

    Args:
        v_rms: Mains voltage at the moment of the switch.
        before: Appliances active immediately before the edge.
        after: Appliances active immediately after the edge.
        switched: The appliance that switched. Supplies the inrush and settle
            characteristics, which are properties of the load rather than of
            the aggregate.

    Note there is no ``edge`` parameter. That absence is the structural
    guarantee that the classifier cannot see edge direction.
    """
    m_before = metrics_from_phasors(v_rms, sum_phasors(before))
    m_after = metrics_from_phasors(v_rms, sum_phasors(after))

    delta_p = abs(m_after.p - m_before.p)
    delta_q1 = abs(m_after.q1 - m_before.q1)
    delta_dist = abs(m_after.dist - m_before.dist)
    delta_s = abs(m_after.s - m_before.s)
    delta_irms = abs(m_after.irms - m_before.irms)
    delta_crest = abs(m_after.crest - m_before.crest)

    fundamental_va = math.hypot(delta_p, delta_q1)
    pf_disp = (delta_p / fundamental_va) if fundamental_va > 0.0 else 1.0
    pf_true = (delta_p / delta_s) if delta_s > 0.0 else 1.0

    return {
        "delta_p": delta_p,
        "delta_q1": delta_q1,
        "delta_dist": delta_dist,
        "delta_s": delta_s,
        "pf_disp": min(1.0, pf_disp),
        "pf_true": min(1.0, pf_true),
        "delta_irms": delta_irms,
        "delta_crest": delta_crest,
        "h3_h1": _harmonic_ratio(switched, 3),
        "h5_h1": _harmonic_ratio(switched, 5),
        "h7_h1": _harmonic_ratio(switched, 7),
        "inrush_ratio": switched.inrush_ratio,
        "settle_cycles": switched.settle_cycles,
        "log_delta_p": math.log1p(delta_p),
    }
