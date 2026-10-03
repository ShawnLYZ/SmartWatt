"""The eight scripted runs.

A scenario is a list of (offset_seconds, action) steps. Actions switch
appliances on and off; telemetry is emitted at 1 Hz throughout.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .appliances import DEFAULT_APPLIANCES_PATH, Appliance, load_appliances
from .payload import SimState, build_event, build_telemetry

V_RMS = 240.0
FREQ = 50.0
FLOOR_W = 6.0
NOISE_W = 1.8

TRAINED = {"kettle", "desk_fan", "incandescent_lamp", "led_bulb", "laptop_charger"}


@dataclass(frozen=True, slots=True)
class Step:
    at: float
    appliance: str
    edge: str
    rejected: bool = False
    ambiguous: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    duration_s: float
    steps: tuple[Step, ...]
    #: Multiplies the wall-clock energy accumulation. Only ``cliff`` uses it,
    #: to compress a month into a short run.
    energy_scale: float = 1.0


def _demo_steps() -> tuple[Step, ...]:
    return (
        Step(3, "incandescent_lamp", "on"),
        Step(9, "desk_fan", "on"),
        Step(15, "led_bulb", "on"),
        Step(21, "laptop_charger", "on"),
        Step(27, "kettle", "on"),
        Step(57, "kettle", "off"),
        Step(63, "led_bulb", "off"),
        Step(69, "clothes_iron", "on", rejected=True, reason="distance_threshold"),
        Step(99, "clothes_iron", "off", rejected=True, reason="distance_threshold"),
        Step(105, "desk_fan", "off"),
        Step(111, "laptop_charger", "off"),
        Step(117, "incandescent_lamp", "off"),
    )


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(
        "baseline", "Quiet circuit, noise only. US52.", 30.0, ()
    ),
    "single": Scenario(
        "single", "One appliance on then off, clean edges.", 40.0,
        (Step(5, "kettle", "on"), Step(35, "kettle", "off")),
    ),
    "overlap": Scenario(
        "overlap", "A second appliance switches while the first runs. US54.", 45.0,
        (
            Step(4, "incandescent_lamp", "on"),
            Step(12, "desk_fan", "on"),
            Step(28, "desk_fan", "off"),
            Step(40, "incandescent_lamp", "off"),
        ),
    ),
    "simultaneous": Scenario(
        "simultaneous", "Two edges inside one settle window. US18.", 30.0,
        (
            Step(10, "desk_fan", "on", ambiguous=True, reason="overlapping_edges"),
            Step(10, "led_bulb", "on", ambiguous=True, reason="overlapping_edges"),
            Step(25, "desk_fan", "off"),
            Step(25, "led_bulb", "off"),
        ),
    ),
    "unknown": Scenario(
        "unknown", "Clothes iron, never taught. US13, US14.", 40.0,
        (
            Step(6, "clothes_iron", "on", rejected=True, reason="distance_threshold"),
            Step(32, "clothes_iron", "off", rejected=True, reason="distance_threshold"),
        ),
    ),
    "below-floor": Scenario(
        "below-floor", "Phone charger under the detection floor. US9.", 30.0,
        (
            Step(8, "phone_charger", "on", rejected=True, reason="below_floor"),
            Step(24, "phone_charger", "off", rejected=True, reason="below_floor"),
        ),
    ),
    "cliff": Scenario(
        "cliff",
        "An accelerated month crossing 400 kWh. S4's Cliff Gauge needs this.",
        120.0,
        (Step(2, "kettle", "on"), Step(118, "kettle", "off")),
        energy_scale=2400.0,
    ),
    "demo": Scenario(
        "demo", "The full five-minute demonstration run.", 125.0, _demo_steps()
    ),
}


def run(
    scenario_name: str,
    *,
    appliances: dict[str, Appliance] | None = None,
    start_ts: float = 1754035200.0,
) -> Iterator[tuple[str, dict]]:
    """Yield ``("telemetry" | "event", payload)`` for one scenario run."""
    scenario = SCENARIOS[scenario_name]
    catalogue = appliances or load_appliances(DEFAULT_APPLIANCES_PATH)
    state = SimState()

    active: list[Appliance] = []
    unknown_ids: dict[str, str] = {}
    next_unknown = 1
    pending = sorted(scenario.steps, key=lambda s: s.at)
    index = 0

    if scenario.name == "cliff":
        state.wh_today = 395_000.0
    previous_session = 0.0

    for second in range(int(scenario.duration_s) + 1):
        ts = start_ts + second

        while index < len(pending) and pending[index].at <= second:
            step = pending[index]
            index += 1
            appliance = catalogue[step.appliance]
            before = list(active)

            if step.edge == "on":
                active.append(appliance)
            else:
                active = [a for a in active if a.id != appliance.id]

            attributed_to: str | None
            if step.ambiguous:
                attributed_to = None
            elif step.rejected:
                if step.edge == "on":
                    unknown_ids[appliance.id] = f"unknown_{next_unknown}"
                    next_unknown += 1
                attributed_to = unknown_ids.get(appliance.id)
            else:
                attributed_to = appliance.id

            yield "event", build_event(
                ts, step.edge, appliance, before, list(active), state,
                v_rms=V_RMS, trained_labels=TRAINED,
                rejected=step.rejected, ambiguous=step.ambiguous,
                reason=step.reason, attributed_to=attributed_to,
            )

            if step.edge == "off":
                unknown_ids.pop(appliance.id, None)

        telemetry = build_telemetry(
            ts, V_RMS, FREQ, active, state,
            floor_w=FLOOR_W, noise_w=NOISE_W, unknown_ids=unknown_ids,
        )

        if scenario.energy_scale != 1.0:
            session = telemetry["energy"]["wh_session"]
            state.wh_today += (
                (session - previous_session)
                * (scenario.energy_scale - 1.0)
            )
            previous_session = session
            telemetry["energy"]["wh_today"] = state.wh_today

        yield "telemetry", telemetry
