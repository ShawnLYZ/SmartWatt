"""Construct contract-shaped telemetry and event payloads.

The simulator does no DSP. It computes payloads directly from the appliance
model, and separately renders samples from that same model. The server
cannot tell these payloads from the device's, which is the whole point.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .aggregate import aggregate, metrics_from_phasors, sum_phasors
from .appliances import Appliance
from .features import derive_features

SIM_SOURCE = "simulator"

_TELEMETRY_SCHEMA = "smartwatt.telemetry.v1"
_EVENT_SCHEMA = "smartwatt.event.v1"

#: Peak amps the low-range CT can represent before the ADC clips:
#: (adc_max - adc_mid) * i_cal_low, derived in the S6 spec. A real
#: device selects range on measured current, never on real power.
_LOW_RANGE_PEAK_A = 8.246


@dataclass(slots=True)
class SimState:
    """Mutable run state: sequence numbers, energy accumulators, on-times."""

    seq: int = 0
    wh_session: float = 0.0
    wh_today: float = 0.0
    since: dict[str, float] = field(default_factory=dict)
    last_ts: float | None = None
    rng: random.Random = field(default_factory=lambda: random.Random(20260813))

    def next_seq(self) -> int:
        value = self.seq
        self.seq += 1
        return value


def build_telemetry(
    ts: float,
    v_rms: float,
    freq: float,
    active: Sequence[Appliance],
    state: SimState,
    *,
    floor_w: float,
    noise_w: float = 0.0,
    unknown_ids: Mapping[str, str] | None = None,
) -> dict:
    """Build one 1 Hz telemetry payload.

    Args:
        unknown_ids: Maps an appliance id to the ``unknown_N`` id the tracker
            would have assigned it, for loads the classifier rejected. Their
            energy is still attributed, which is what makes US14 hold.
    """
    unknown_ids = unknown_ids or {}

    # Measurement noise enters as a current, the way it does in the real
    # signal chain. Adding it to P alone would publish a payload with
    # |P| > S, which no device can produce.
    if noise_w:
        phasors = sum_phasors(active)
        jitter = state.rng.gauss(0.0, noise_w) / v_rms
        phasors[1] = phasors.get(1, 0j) + complex(jitter, 0.0)
        metrics = metrics_from_phasors(v_rms, phasors)
    else:
        metrics = aggregate(v_rms, active)

    measured_p = metrics.p

    if state.last_ts is not None:
        hours = max(0.0, ts - state.last_ts) / 3600.0
        contribution = max(0.0, measured_p) * hours
        state.wh_session += contribution
        state.wh_today += contribution
    state.last_ts = ts

    entries = []
    attributed = 0.0
    for appliance in active:
        state.since.setdefault(appliance.id, ts)
        watts = aggregate(v_rms, [appliance]).p
        attributed += watts
        entries.append({
            "id": unknown_ids.get(appliance.id, appliance.id),
            "w": watts,
            "since": state.since[appliance.id],
        })

    live = {a.id for a in active}
    for stale in set(state.since) - live:
        del state.since[stale]

    return {
        "schema": _TELEMETRY_SCHEMA,
        "ts": ts,
        "source": SIM_SOURCE,
        "seq": state.next_seq(),
        "fingerprint_id": None,
        "electrical": {
            "vrms": v_rms,
            "irms": metrics.irms,
            "p": measured_p,
            "q1": metrics.q1,
            "dist": metrics.dist,
            "s": metrics.s,
            "pf_true": metrics.pf_true,
            "pf_disp": metrics.pf_disp,
            "freq": freq,
            "range": (
                "high"
                if metrics.irms * metrics.crest > _LOW_RANGE_PEAK_A
                else "low"
            ),
        },
        "attribution": {
            "active": entries,
            # Never clamped. Non-negotiable #2.
            "residual_w": measured_p - attributed,
            "floor_w": floor_w,
        },
        "energy": {
            "wh_session": state.wh_session,
            "wh_today": state.wh_today,
        },
        "health": None,
    }


def _neighbours(label: str | None, trained_labels: set[str], rejected: bool) -> list[dict]:
    """Three nearest training events. Far away when the load is untrained."""
    others = sorted(trained_labels - {label})
    base = 4.8 if rejected else 0.31
    picks: list[str] = []
    if label is not None and not rejected:
        picks = [label, label, label]
    else:
        picks = (others * 3)[:3] if others else ["kettle", "kettle", "kettle"]
    return [
        {
            "label": picks[n],
            "distance": base + 0.13 * n,
            "training_id": f"{picks[n]}-on-{n + 1:03d}",
        }
        for n in range(3)
    ]


def build_event(
    ts: float,
    edge: str,
    switched: Appliance,
    before: Sequence[Appliance],
    after: Sequence[Appliance],
    state: SimState,
    *,
    v_rms: float,
    trained_labels: set[str],
    rejected: bool = False,
    ambiguous: bool = False,
    reason: str | None = None,
    attributed_to: str | None = ...,  # type: ignore[assignment]
) -> dict:
    """Build one switching-event payload.

    ``attributed_to`` defaults to the classifier's label. Pass it explicitly
    to model the tracker's state-consistency filter overriding the classifier
    (US17) or assigning an ``unknown_N`` id (US13, US14).
    """
    clean = not rejected and not ambiguous
    label = switched.id if clean else None
    if attributed_to is ...:
        attributed_to = label

    return {
        "schema": _EVENT_SCHEMA,
        "ts": ts,
        "source": SIM_SOURCE,
        "seq": state.next_seq(),
        "edge": edge,
        "label": label,
        "confidence": 0.94 if clean else 0.0,
        "rejected": rejected,
        "ambiguous": ambiguous,
        "reason": reason,
        "attributed_to": attributed_to,
        "features": derive_features(v_rms, before, after, switched),
        "neighbours": _neighbours(label, set(trained_labels), rejected),
    }
