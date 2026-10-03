"""The report.

It states each of the NINE published targets and says plainly whether it
was met. Targets are not lowered before measurement: missing a stated
target and saying so reads as strong; lowering one before measuring reads
as weak.

R9 (non-negotiable #2): nothing estimated or unmeasured is presented as
measured. The rejection threshold section below states its derivation and
value unconditionally, but only claims validation against an untrained
load when a `rejection` figure was actually measured - never named loads,
never an unconditional claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .harness import REJECTED, CvResult


@dataclass(frozen=True, slots=True)
class Target:
    key: str
    name: str
    stated: float
    stretch: float
    #: ">=" means higher is better; "<=" means lower is better.
    comparison: str
    unit: str = ""


#: Carried forward from SmartWatt.md 13.3 UNCHANGED AND UNLOWERED.
TARGETS: tuple[Target, ...] = (
    Target("power_accuracy", "Power accuracy, P > 100 W", 0.02, 0.01, "<=", ""),
    Target("detection_floor", "Detection floor", 10.0, 5.0, "<=", "W"),
    Target("event_recall", "Event recall, dP > 20 W", 0.95, 0.98, ">=", ""),
    Target("event_precision", "Event precision", 2.0, 0.5, "<=", "/h"),
    Target("classification", "Classification top-1", 0.92, 0.96, ">=", ""),
    Target("rejection", "Rejection of untrained loads", 0.90, 0.95, ">=", ""),
    Target("nde", "Per-appliance energy error (NDE)", 0.12, 0.06, "<=", ""),
    Target("latency", "Event to dashboard latency", 3.0, 1.5, "<=", "s"),
    Target("overruns", "Sampler overruns over 1 h", 0.0, 0.0, "<=", ""),
)


@dataclass(frozen=True, slots=True)
class Measured:
    key: str
    value: float | None
    note: str = ""


def _verdict(target: Target, value: float | None) -> str:
    if value is None:
        return "not measured"
    met = value <= target.stated if target.comparison == "<=" else value >= target.stated
    return "MET" if met else "NOT MET"


def _row(target: Target, value: float | None, label: str | None = None) -> str:
    shown = "—" if value is None else f"{value:g}{target.unit}"
    stated = f"{target.comparison} {target.stated:g}{target.unit}"
    return f"| {label or target.name} | {stated} | {shown} | {_verdict(target, value)} |"


def build_report(
    cv: CvResult,
    live_accuracy: float | None,
    importance: dict[str, float],
    threshold: float,
    measured: list[Measured],
    conditions: str,
    *,
    live_plain_accuracy: float | None = None,
    limitations: list[str] | None = None,
) -> str:
    """R23: ``cv.accuracy`` and ``live_accuracy`` are the DEVICE's figures -
    a rejected event is not a correct one - and they alone are verdicted.
    ``cv.plain_accuracy`` and ``live_plain_accuracy`` (plain k-NN top-1, no
    rejection) are printed alongside, labelled, never verdicted and never
    averaged with anything."""
    values = {entry.key: entry.value for entry in measured}
    limitations = list(limitations or []) + [
        f"Class `{label}` has a single training row. It is in every training "
        "fold, as the device would hold it, but it cannot be held out, so no "
        "cross-validated figure covers it."
        for label in cv.unscored
    ]

    lines: list[str] = []
    lines.append("# SmartWatt — measurement report")
    lines.append("")
    lines.append(f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Published targets")
    lines.append("")
    lines.append(
        "Every stated target appears below with a plain verdict. Targets were "
        "not lowered before measurement."
    )
    lines.append("")
    lines.append("| Metric | Stated | Measured | Verdict |")
    lines.append("|---|---|---|---|")

    for target in TARGETS:
        if target.key == "classification":
            # Ten rows for nine targets: one stated target, reported twice,
            # because cross-validated and live must never be averaged.
            # Both are the device's figure, rejection included (R23).
            lines.append(_row(
                target, cv.accuracy,
                "Classification top-1 (cross-validated, with rejection)",
            ))
            lines.append(_row(
                target, live_accuracy,
                "Classification top-1 (live held-out, with rejection)",
            ))
        else:
            lines.append(_row(target, values.get(target.key)))

    plain_live = (
        "not measured" if live_plain_accuracy is None
        else f"{live_plain_accuracy:.3f}"
    )
    lines.append("")
    lines.append(
        "Classification is verdicted on what the device does: an event it "
        "rejects is not a correct answer. For reference only, plain k-NN "
        "top-1 without the device's rejection, not verdicted: "
        f"cross-validated {cv.plain_accuracy:.3f}; live held-out {plain_live}."
    )

    lines.append("")
    lines.append("## The cross-validated versus live gap")
    lines.append("")
    if live_accuracy is None:
        lines.append(
            "No live held-out session has been run yet, so no gap can be reported."
        )
    else:
        gap = cv.accuracy - live_accuracy
        lines.append(
            f"Cross-validated accuracy is {cv.accuracy:.3f}; live held-out "
            f"accuracy is {live_accuracy:.3f}, a gap of {gap:.3f}."
        )
        lines.append("")
        lines.append(
            "These are reported separately and are never averaged into one "
            "figure. Live is expected to be lower. The gap is explained from "
            "the logged session conditions below rather than smoothed away."
        )
        lines.append("")
        lines.append(conditions or "_No session conditions recorded._")

    lines.append("")
    lines.append("## Confusion matrix")
    lines.append("")
    lines.append(
        "| actual \\ predicted | " + " | ".join([*cv.labels, REJECTED]) + " |"
    )
    lines.append("|---" * (len(cv.labels) + 2) + "|")
    for index, label in enumerate(cv.labels):
        row = " | ".join(str(value) for value in cv.confusion[index])
        lines.append(f"| **{label}** | {row} |")

    lines.append("")
    lines.append("## Per-fold accuracy")
    lines.append("")
    lines.append(
        "With rejection: " + ", ".join(f"{value:.3f}" for value in cv.per_fold)
    )
    lines.append("")
    lines.append(
        "Plain k-NN, without rejection: "
        + ", ".join(f"{value:.3f}" for value in cv.plain_per_fold)
    )

    lines.append("")
    lines.append("## Rejection threshold")
    lines.append("")
    lines.append(
        f"Derived as the 95th percentile of within-class centroid distance "
        f"multiplied by 1.5: **{threshold:.4f}**. Never hardcoded from "
        f"intuition."
    )
    rejection_value = values.get("rejection")
    if rejection_value is None:
        lines.append(
            "Validation of this threshold against a load the system was "
            "never taught has not been measured yet."
        )
    else:
        lines.append(
            f"It was validated against untrained loads, with a measured "
            f"rejection rate of {rejection_value:g}."
        )

    if importance:
        lines.append("")
        lines.append("## Permutation feature importance")
        lines.append("")
        lines.append("| Feature | Accuracy drop when shuffled |")
        lines.append("|---|---|")
        for name, value in sorted(importance.items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{name}` | {value:.4f} |")

    if limitations:
        lines.append("")
        lines.append("## Limitations of this run")
        lines.append("")
        for note in limitations:
            lines.append(f"- {note}")

    lines.append("")
    lines.append("## Signature scatter")
    lines.append("")
    lines.append("![Appliance signatures](scatter.png)")
    lines.append("")
    return "\n".join(lines)
