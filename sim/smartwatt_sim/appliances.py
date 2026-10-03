"""Appliance models as harmonic current phasors.

An appliance is not a wattage. It is a set of harmonic current phasors
relative to the voltage fundamental. That choice is what lets one model
drive both the payload publisher and the waveform renderer, and it is what
makes composition across parallel loads physically correct.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APPLIANCES_PATH = Path(__file__).resolve().parent.parent / "data" / "appliances.toml"

#: Reference mains conditions the appliance phasors are defined against.
NOMINAL_VRMS = 240.0
NOMINAL_FREQ = 50.0


@dataclass(frozen=True, slots=True)
class Harmonic:
    """One harmonic current component.

    Attributes:
        h: Harmonic order. 1 is the fundamental.
        i_rms: Amps rms at this order.
        phase: Lag of this current harmonic behind the voltage fundamental,
            in radians. Positive is inductive at the fundamental.
    """

    h: int
    i_rms: float
    phase: float


@dataclass(frozen=True, slots=True)
class Appliance:
    id: str
    harmonics: tuple[Harmonic, ...]
    inrush_ratio: float
    inrush_decay_cycles: int
    settle_cycles: int
    protected: bool
    heating: bool
    trained: bool


def load_appliances(path: Path = DEFAULT_APPLIANCES_PATH) -> dict[str, Appliance]:
    """Load appliance definitions from TOML, keyed by id."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Appliance] = {}
    for appliance_id, body in raw.items():
        harmonics = tuple(
            Harmonic(h=int(item["h"]), i_rms=float(item["i_rms"]), phase=float(item["phase"]))
            for item in body["harmonics"]
        )
        out[appliance_id] = Appliance(
            id=appliance_id,
            harmonics=harmonics,
            inrush_ratio=float(body["inrush_ratio"]),
            inrush_decay_cycles=int(body["inrush_decay_cycles"]),
            settle_cycles=int(body["settle_cycles"]),
            protected=bool(body["protected"]),
            heating=bool(body["heating"]),
            trained=bool(body["trained"]),
        )
    return out
