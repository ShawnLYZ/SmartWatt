"""The guided setup wizard.

Two properties carry it:

  * EVERY capture requires an explicit confirmation before it counts. A
    capture that silently failed and one that succeeded must never look the
    same, or the training file quietly fills with gaps. Nothing is written
    to the file until it is confirmed.
  * The verification round is a GATE, not a summary. Failing it returns to
    capture rather than completing with a warning.

Progress is DERIVED FROM THE TRAINING FILE, never kept in memory: a row with
a non-empty concurrent_ids is an overlapped capture, one without is a quiet
capture. So a restarted server, or a failed verification, picks up where the
file says the training stands instead of starting the quotas again.

Nothing here is taken from the client that the system can measure itself
(non-negotiable #2): the baseline, and each capture's background, Vrms and
frequency, come from STORED TELEMETRY, and the push is confirmed by the
DEVICE's own report of the table it loaded, not by the broker accepting a
message.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from smartwatt_analysis.fingerprints import (
    FEATURE_COLUMNS,
    Fingerprint,
    MalformedFingerprints,
    append_fingerprint,
    read_fingerprints,
)

from .publish_fingerprints import (
    MAX_LABEL,
    MAX_TRAINING_ID,
    UnpushableTable,
    build_fingerprints_payload,
    is_emittable_label,
    publish_fingerprints,
)
from .store import Store

#: Confirmed captures needed per (class, edge) in EACH of the quiet and the
#: overlapped steps: 5 ON + 5 OFF quiet, 5 ON + 5 OFF overlapped, so 20 per
#: class. Per (class, edge) rather than per class, so ten ON captures of one
#: class cannot stand in for its OFF edge.
PER_EDGE_QUOTA = 5
EDGES = ("on", "off")
TRAINED_CLASSES = (
    "kettle", "desk_fan", "incandescent_lamp", "led_bulb", "laptop_charger",
)
#: US55: three correct in a row, or back to capture.
VERIFY_STREAK = 3
#: How far back an event-less capture or verify looks for its event.
_EVENT_LOOKBACK = 50
#: A capture's conditions come from the newest telemetry at or before its
#: event, no older than this. Telemetry is 1 Hz, so 5 s tolerates a few
#: lost samples without reaching back to a different circuit state.
CONDITIONS_WINDOW_S = 5.0
#: Tracker reasons (contract/schemas/event.schema.json, tracker.cpp) that
#: mark an edge the classifier never cleanly saw. `distance_threshold` is
#: NOT here: a settled edge the classifier rejected is exactly what an
#: untrained load looks like, which is what the wizard is capturing.
UNUSABLE_REASONS = {
    "below_floor": "below the detection floor",
    "no_settle": "never settled",
    "overlapping_edges": "overlapped another edge",
}
_CAPTURE_STEPS = ("quiet", "overlapped")


class Step(StrEnum):
    BASELINE = "baseline"
    QUIET = "quiet"
    OVERLAPPED = "overlapped"
    PUSH = "push"
    VERIFY = "verify"
    DONE = "done"


class WrongStep(RuntimeError):
    """The action is not available in the wizard's current step."""


class NoEventToVerify(LookupError):
    """verify() was asked to read the device's answer and none has arrived."""


class NoTelemetry(LookupError):
    """A measurement was asked of stored telemetry and there is none."""


@dataclass(slots=True)
class CaptureResult:
    accepted: bool
    reason: str | None
    training_id: str | None
    captured: int
    required: int
    #: R25: the event the capture bound (or refused), so the confirmation
    #: prompt can show a wrong binding looking different from a right one.
    #: None when no event was bound at all.
    event_ts: float | None = None
    delta_p: float | None = None
    event_reason: str | None = None


def _category(row: Fingerprint) -> Step:
    return Step.OVERLAPPED if row.concurrent_ids.strip() else Step.QUIET


def _unusable(event: dict) -> str | None:
    """Why this event cannot be a training row, or None if it can."""
    reason = event.get("reason")
    if reason in UNUSABLE_REASONS:
        return (
            f"the bound {event.get('edge', '')} edge was {UNUSABLE_REASONS[reason]} "
            f"({reason}); switch the appliance again"
        )
    if event.get("ambiguous"):
        return "the bound edge is ambiguous; switch the appliance again"
    return None


class Wizard:
    def __init__(
        self,
        store: Store,
        path: Path,
        clock: Callable[[], float] = time.time,
        classes: tuple[str, ...] = TRAINED_CLASSES,
        publisher: Callable[[str, str, bool], object] | None = None,
    ) -> None:
        for label in classes:
            if not is_emittable_label(label):
                raise ValueError(
                    f"trained class {label!r} is not a label the device loads "
                    f"([a-z][a-z0-9_]*, at most {MAX_LABEL} characters, not "
                    "unknown_<n>)"
                )
        self._store = store
        self._path = Path(path)
        self._clock = clock
        self._classes = tuple(classes)
        # R21: the same publisher signature as api.py's
        # `app.state.fingerprint_publisher` -- the push is performed
        # through this injected callable, never through MQTT directly, so
        # the architecture scan's publish-reference search stays confined
        # to ingest.py and api.py (see publish_fingerprints.py's own
        # docstring). `None` (the default, used by tests that never reach
        # PUSH) behaves exactly like a publisher that always reports no
        # client: a built payload but an unpublished one.
        self._publisher = publisher
        self._step = Step.BASELINE
        self._unconfirmed: dict[str, Fingerprint] = {}
        self._streak = 0
        self._baseline_w: float | None = None
        #: Why the last advance() returned False, for api.py's 409.
        self.refusal: str | None = None
        # Watermarks: an event-less capture or verify binds only an event
        # NEWER than these, so no event is ever used twice and nothing from
        # before the step began is picked up. None means "anything".
        self._capture_after: float | None = None
        self._verify_after: float | None = None
        # R21: the last push attempt's outcome. `state()` only SURFACES
        # this while the step is still PUSH -- but the raw value survives
        # past that so `last_push_result()` can still report it to api.py's
        # advance route on exactly the call that leaves PUSH.
        self._push_outcome: dict | None = None
        # The id of the table that push attempt published.
        self._pushed_id: str | None = None

    # -- the training file --------------------------------------------------

    def rows(self) -> list[Fingerprint]:
        """Raises MalformedFingerprints for a file that cannot be read."""
        if not self._path.exists():
            return []
        return read_fingerprints(self._path)

    def _read(self) -> tuple[list[Fingerprint], str | None]:
        """The rows, or none and the reason the file cannot be read. A bad
        hand edit is reported, never a crash (R27c)."""
        try:
            return self.rows(), None
        except MalformedFingerprints as exc:
            return [], f"{self._path.name} cannot be read: {exc}"

    def _breakdown(
        self, rows: list[Fingerprint]
    ) -> dict[Step, dict[str, dict[str, int]]]:
        counts = {
            step: {label: {edge: 0 for edge in EDGES} for label in self._classes}
            for step in (Step.QUIET, Step.OVERLAPPED)
        }
        for row in rows:
            if row.label in self._classes and row.edge in EDGES:
                counts[_category(row)][row.label][row.edge] += 1
        return counts

    def _counts(self) -> tuple[int, int, bool]:
        """(confirmed, required, quotas met) for the current step."""
        if self._step not in (Step.QUIET, Step.OVERLAPPED):
            return 0, 0, True
        rows, error = self._read()
        table = self._breakdown(rows)[self._step]
        confirmed = sum(n for edges in table.values() for n in edges.values())
        required = PER_EDGE_QUOTA * len(EDGES) * len(self._classes)
        met = error is None and all(
            n >= PER_EDGE_QUOTA for edges in table.values() for n in edges.values()
        )
        return confirmed, required, met

    def _expected_id(self) -> tuple[str | None, dict | None, str | None]:
        """(id, payload, refusal) of the table on disk as the device would
        load it. No id when the table cannot be built."""
        rows, error = self._read()
        if error is not None:
            return None, None, error
        try:
            payload = build_fingerprints_payload(rows)
        except UnpushableTable as exc:
            return None, None, str(exc)
        return payload["fingerprint_id"], payload, None

    def _device_id(self) -> str | None:
        """What the device says it loaded: the NEWEST telemetry's id."""
        newest = self._store.latest_telemetry()
        return newest.get("fingerprint_id") if newest else None

    # -- state ---------------------------------------------------------------

    def state(self) -> dict:
        confirmed, required, _ = self._counts()
        rows, error = self._read()
        breakdown = self._breakdown(rows)
        state = {
            "step": self._step,
            # Confirmed rows for THIS step, over the trained classes. A raw
            # count: "breakdown" is what says which (class, edge) is short.
            "confirmed": confirmed,
            "required": required,
            "per_edge_required": PER_EDGE_QUOTA,
            "breakdown": {
                "quiet": breakdown[Step.QUIET],
                "overlapped": breakdown[Step.OVERLAPPED],
            },
            "awaiting_confirmation": len(self._unconfirmed),
            "streak": self._streak,
            "streak_required": VERIFY_STREAK,
            "baseline_w": self._baseline_w,
            "fingerprints_path": str(self._path.resolve()),
            "classes": list(self._classes),
            # R27c: a hand edit that broke the file, stated, not a 500.
            "file_error": error,
            # R21: null before any push has been attempted THIS time the
            # wizard is in PUSH, and null again once it has left PUSH.
            "push": self._push_outcome if self._step is Step.PUSH else None,
            # R22: the table's id and the device's. Null outside PUSH and
            # VERIFY; the device's is null until it reports one.
            "expected_fingerprint_id": None,
            "device_fingerprint_id": None,
        }
        if self._step in (Step.PUSH, Step.VERIFY):
            state["expected_fingerprint_id"] = self._expected_id()[0]
            state["device_fingerprint_id"] = self._device_id()
        return state

    def begin(self) -> None:
        self._step = Step.BASELINE
        self._unconfirmed.clear()
        self._streak = 0
        self._baseline_w = None
        self.refusal = None
        self._capture_after = None
        self._verify_after = None
        self._push_outcome = None
        self._pushed_id = None

    def reset(self) -> None:
        self.begin()

    def record_baseline(self) -> float:
        """US52: the system learns what a quiet circuit looks like -- from
        the newest stored telemetry's total real power, never a typed or
        defaulted figure."""
        if self._step is not Step.BASELINE:
            raise WrongStep(f"not recording a baseline in step {self._step}")
        newest = self._store.latest_telemetry()
        if newest is None or newest.get("p") is None:
            raise NoTelemetry(
                "no telemetry has been stored yet, so there is no measured "
                "circuit power to record; check the device is publishing"
            )
        self._baseline_w = float(newest["p"])
        return self._baseline_w

    def last_push_result(self) -> dict | None:
        """R21: the most recent push attempt's outcome, regardless of the
        CURRENT step -- readable for one call past the transition out of
        PUSH, which is what lets api.py's advance route mirror it on
        exactly the call that leaves PUSH for VERIFY."""
        return self._push_outcome

    def _attempt_push(self, payload: dict | None, refusal: str | None) -> dict:
        """Publishes the table RETAINED through the injected publisher.
        Never claims success unless `publish_fingerprints` itself reports
        True. Publishing is not the gate out of PUSH -- the device's id is
        -- but its outcome is always shown."""
        if payload is None:
            self._pushed_id = None
            return {"published": False, "fingerprint_id": None, "reason": refusal}
        published = (
            publish_fingerprints(self._publisher, payload)
            if self._publisher is not None
            else False
        )
        self._pushed_id = payload["fingerprint_id"] if published else None
        return {
            "published": published,
            "fingerprint_id": payload["fingerprint_id"] if published else None,
            "reason": None,
        }

    def _newest_event_ts(self) -> float | None:
        newest = self._store.events(limit=1)
        return float(newest[0]["ts"]) if newest else None

    def _leave_capture(self) -> None:
        # R26: a capture belongs to the step it was made in. Leaving that
        # step drops every unconfirmed one, so none can be confirmed later
        # into a table that has already moved on.
        self._unconfirmed.clear()

    def _enter_capture(self) -> None:
        self._step = Step.QUIET
        # R21: any push already attempted belonged to the table as it
        # stood before this return to capture.
        self._push_outcome = None
        self._pushed_id = None
        self._capture_after = self._newest_event_ts()

    # -- capture -------------------------------------------------------------

    def _refuse(self, reason: str, event: dict | None = None) -> CaptureResult:
        confirmed, required, _ = self._counts()
        return CaptureResult(False, reason, None, confirmed, required,
                             *self._describe(event))

    @staticmethod
    def _describe(event: dict | None) -> tuple[float | None, float | None, str | None]:
        if event is None:
            return None, None, None
        features = event.get("features") or {}
        delta_p = features.get("delta_p")
        ts = event.get("ts")
        return (
            float(ts) if ts is not None else None,
            float(delta_p) if delta_p is not None else None,
            event.get("reason"),
        )

    def _bind_event(self, edge: str) -> dict | None:
        """The newest stored event on this edge, newer than the last capture."""
        for event in self._store.events(limit=_EVENT_LOOKBACK):
            if self._capture_after is not None and event["ts"] <= self._capture_after:
                return None
            if event["edge"] == edge:
                return event
        return None

    def _conditions_at(self, ts: float) -> tuple[float | None, float | None, float | None]:
        """(background_w, vrms_mean, freq_mean) from the newest telemetry at
        or before ``ts``, no older than CONDITIONS_WINDOW_S. None for each
        when nothing measured them -- a blank cell, never a zero."""
        row = self._store.telemetry_at_or_before(ts, CONDITIONS_WINDOW_S)
        if row is None:
            return None, None, None

        def value(key: str) -> float | None:
            return None if row.get(key) is None else float(row[key])

        return value("p"), value("vrms"), value("freq")

    def _next_training_id(
        self, rows: list[Fingerprint], label: str, edge: str
    ) -> str | None:
        taken = {row.training_id for row in rows} | set(self._unconfirmed)
        for n in range(1, 1000):
            candidate = f"{label}-{edge}-{n:03d}"
            if candidate not in taken:
                return candidate
        return None

    def capture(
        self, label: str, edge: str, event: dict | None, conditions: dict | None
    ) -> CaptureResult:
        """``conditions`` supplies only session_id, concurrent_ids and notes.
        background_w, vrms_mean and freq_mean in it are IGNORED: they are
        measured, from stored telemetry (R24)."""
        conditions = conditions or {}
        if self._step not in (Step.QUIET, Step.OVERLAPPED):
            return self._refuse(f"not capturing in step {self._step}")

        rows, error = self._read()
        if error is not None:
            return self._refuse(error)

        if len(label) > MAX_LABEL:
            return self._refuse(
                f"label {label!r} is longer than {MAX_LABEL} characters, the "
                "most the device's table holds"
            )

        if label not in self._classes:
            return self._refuse(f"{label} is not a trained class")

        if edge not in EDGES:
            return self._refuse(f"edge {edge!r} is not one of {EDGES}")

        if self._step is Step.OVERLAPPED and not conditions.get("concurrent_ids"):
            # A model trained only against silence fails the moment two loads
            # overlap, so this step refuses a quiet capture outright.
            return self._refuse(
                "this step needs another appliance already running; set "
                "concurrent_ids"
            )

        if event is None:
            event = self._bind_event(edge)
            if event is None:
                return self._refuse("no edge detected")
        elif event.get("edge", edge) != edge:
            return self._refuse(
                f"the event is an {event.get('edge')} edge, not the {edge} edge "
                "being captured",
                event,
            )

        ts = float(event.get("ts", self._clock()))
        # Consumed whether it is used or refused: an unusable edge is never
        # offered again, and the next switch is newer anyway.
        if self._capture_after is None or ts > self._capture_after:
            self._capture_after = ts

        unusable = _unusable(event)
        if unusable is not None:
            return self._refuse(unusable, event)

        features = event.get("features") or {}
        missing = set(FEATURE_COLUMNS) - set(features)
        if missing:
            return self._refuse(f"event is missing features: {sorted(missing)}", event)

        training_id = self._next_training_id(rows, label, edge)
        if training_id is None or len(training_id) > MAX_TRAINING_ID:
            return self._refuse(f"no training_id left for {label} {edge}", event)

        background_w, vrms_mean, freq_mean = self._conditions_at(ts)
        self._unconfirmed[training_id] = Fingerprint(
            training_id=training_id,
            label=label,
            edge=edge,
            features={name: float(features[name]) for name in FEATURE_COLUMNS},
            ts=ts,
            session_id=str(conditions.get("session_id", "")),
            background_w=background_w,
            concurrent_ids=str(conditions.get("concurrent_ids", "")),
            vrms_mean=vrms_mean,
            freq_mean=freq_mean,
            notes=str(conditions.get("notes", "")),
        )
        confirmed, required, _ = self._counts()
        return CaptureResult(True, None, training_id, confirmed, required,
                             *self._describe(event))

    def confirm(self, training_id: str) -> bool:
        """Nothing is written until the operator confirms it registered.

        Only in the capture step the capture was made in (R26): outside
        QUIET/OVERLAPPED this raises WrongStep, and leaving a capture step
        has already dropped its unconfirmed captures.
        """
        if self._step not in (Step.QUIET, Step.OVERLAPPED):
            raise WrongStep(f"not confirming captures in step {self._step}")
        row = self._unconfirmed.pop(training_id, None)
        if row is None:
            return False
        append_fingerprint(self._path, row)
        return True

    def advance(self) -> bool:
        """False, with ``refusal`` saying why, when the step's requirement is
        unmet. In PUSH it is True either way: a push that has not landed is
        not a refusal, and the caller reads the state to see whether the
        wizard moved on."""
        self.refusal = None
        if self._step is Step.BASELINE:
            if self._baseline_w is None:
                self.refusal = "record a baseline before capturing"
                return False
            self._enter_capture()
            return True
        if self._step in (Step.QUIET, Step.OVERLAPPED):
            error = self._read()[1]
            if error is not None:
                self.refusal = error
                return False
            _, _, met = self._counts()
            if not met:
                self.refusal = "required captures not complete for this step"
                return False
            self._leave_capture()
            if self._step is Step.QUIET:
                self._step = Step.OVERLAPPED
                return True
            self._step = Step.PUSH
            # Falls through: entering PUSH attempts the push immediately.
        if self._step is Step.PUSH:
            expected, payload, refusal = self._expected_id()
            # (Re)publish when nothing has published yet, or when the table
            # on disk is no longer the one that was published.
            if (
                self._push_outcome is None
                or not self._push_outcome["published"]
                or self._pushed_id != expected
            ):
                self._push_outcome = self._attempt_push(payload, refusal)
            # R22: THE GATE. The device, not the broker: its newest
            # telemetry must report the id of the table built here.
            if expected is not None and self._device_id() == expected:
                self._step = Step.VERIFY
                self._streak = 0
                self._verify_after = self._newest_event_ts()
            return True
        return False

    # -- verification --------------------------------------------------------

    def _read_answer(self) -> str:
        for event in self._store.events(limit=1):
            if self._verify_after is not None and event["ts"] <= self._verify_after:
                break
            self._verify_after = float(event["ts"])
            return event["attributed_to"] or event["label"]
        raise NoEventToVerify(
            "no event has arrived since the last verification; switch the "
            "appliance and try again"
        )

    def verify(self, expected: str, actual: str | None = None) -> dict:
        """US55. A gate, not a summary.

        With no ``actual``, the answer is what the device itself said about
        the newest event since the last verification: its attributed_to,
        falling back to its label. (The HTTP route never passes ``actual``.)
        """
        if self._step is not Step.VERIFY:
            raise WrongStep(f"not verifying in step {self._step}")
        if actual is None:
            actual = self._read_answer()

        correct = expected == actual
        if correct:
            self._streak += 1
        else:
            self._streak = 0
            # Back to capture, with every quota already met still met: the
            # counts live in the file. Events from the verification round
            # are not captures.
            self._enter_capture()

        passed = self._streak >= VERIFY_STREAK
        if passed:
            self._step = Step.DONE

        return {
            "expected": expected,
            "actual": actual,
            "correct": correct,
            "streak": self._streak,
            "required": VERIFY_STREAK,
            "passed": passed,
        }
