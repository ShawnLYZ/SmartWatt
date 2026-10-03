"""Two rules, both timers over stored state, both named for what they do.

OCCUPANCY SENSING IS NOT BUILT, and no rule pretends otherwise. The draft
document's idle rule claimed to act on an occupancy signal that does not
exist; its honest form is `left_on`, a timer, which is what this implements.
Occupancy remains the highest-value roadmap item and is out of scope.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .gate import Actor, Command, Gate, Outcome, Result
from .registry import Registry
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuleConfig:
    #: Continuous on-time after which `left_on` warns.
    left_on_seconds: float
    #: (low, high) watts. A draw inside this band counts as standby.
    standby_band_w: tuple[float, float]
    #: Sustained in-band duration after which `standby_draw` warns.
    standby_seconds: float
    #: Warn, wait this long with a cancel available, then act.
    grace_seconds: float
    #: True when these are demonstration values rather than shipped ones.
    #: Surfaced on /api/rules so a short window is never presented as the
    #: shipped default.
    is_demo: bool


SHIPPED_DEFAULTS = RuleConfig(
    left_on_seconds=4 * 3600.0,
    standby_band_w=(1.0, 12.0),
    standby_seconds=6 * 3600.0,
    grace_seconds=60.0,
    is_demo=False,
)

# Ruling B: the draft used left_on_seconds=90.0, which cannot complete the
# `demo` scenario (sim/smartwatt_sim/scenarios.py's _demo_steps): the lamp
# is on from t=3 to t=117, 114 s of continuous on-time. The warn condition
# first holds at t=3+90=93, and 60 s of grace after that would land the
# cut at t=153 (3 + 90 + 60) - but observe() clears the pending the
# instant the lamp goes off at t=117, so the cut could never fire inside
# the run at all. 30 s warns at t=3+30=33 and cuts at t=33+60=93,
# comfortably inside the window. grace_seconds stays at 60 either way: it
# is a binding constraint ("warn, wait a 60-second grace period, then
# act"), not a demo convenience, and the spec draws no distinction between
# the two kinds of default for it.
DEMO_DEFAULTS = RuleConfig(
    left_on_seconds=30.0,
    standby_band_w=(1.0, 12.0),
    standby_seconds=120.0,
    grace_seconds=60.0,
    is_demo=True,
)

#: How old the engine's most recent telemetry may be before its tracked
#: state stops being actionable.
#:
#: `observe()` is the ONLY writer of `_active`, and `_evaluate()` measures
#: durations against the clock, not against the telemetry that produced
#: them. Without a bound, ONE sample claiming the lamp had been on for 10 s
#: followed by total silence still warned, waited out the grace period and
#: published a real cut 5,100 s later -- measured, not hypothesised. In
#: production that is a broker outage while an appliance sits near
#: threshold: a live countdown in the UI for a device the server can no
#: longer see, and then a cut. The blast radius is bounded (the direction
#: is always OFF, and the gate's own guarantees are untouched), but a
#: pending panel asserting a present condition the server cannot
#: substantiate is exactly what non-negotiable #2 forbids.
#:
#: 30 s, chosen against the two things either side of it.
#:
#: NOT SHORTER, because telemetry is 1 Hz: 30 s is thirty consecutive
#: missing samples, well past any jitter, and past MqttIngest's first five
#: reconnect-with-backoff attempts (1+2+4+8+16 s) -- so an ordinary
#: reconnect does not discard live state.
#:
#: NOT LONGER, because it must stay BELOW the grace period, which is 60 s
#: in both configs. That is what makes the pending-cut case decidable
#: instead of a coin flip: a warning is raised on fresh evidence, and if
#: that evidence then stops being renewed, the withdrawal at +30 s always
#: comes before the cut at +60 s. A cut can therefore never fire on
#: telemetry that stopped arriving before its own countdown began. At 90 s
#: -- the first value tried here -- exactly that could happen, and the
#: comment would have been claiming a guarantee the number did not give.
#: test_the_bound_stays_below_every_grace_period pins the relationship so
#: a future RuleConfig cannot quietly break it.
MAX_OBSERVATION_AGE_S = 30.0

#: `left_on` retargeted from the draft's soldering iron. The original is not
#: in the trained set, and the only heating load that is - the kettle - sits
#: in the no-auto-on set and self-terminates at boil, so the rule could never
#: fire honestly. Retargeting also unifies the narrative: the lamp is the
#: appliance the tariff-cliff worked example already names.
#:
#: These are the EXAMPLE household's targets (registry._DEFAULTS). A running
#: server passes the household's own, from appliances.toml's left_on_rule
#: and standby_rule, to the RulesEngine constructor; these apply only when
#: none are passed.
LEFT_ON_TARGETS = ("incandescent_lamp",)
STANDBY_TARGETS = ("led_bulb", "incandescent_lamp", "desk_fan")


@dataclass(slots=True)
class PendingCut:
    id: str
    appliance_id: str
    rule: str
    warned_at: float
    acts_at: float


class RulesEngine:
    def __init__(
        self,
        gate: Gate,
        registry: Registry,
        store: Store,
        config: RuleConfig = SHIPPED_DEFAULTS,
        clock: Callable[[], float] = time.time,
        left_on_targets: tuple[str, ...] | None = None,
        standby_targets: tuple[str, ...] | None = None,
    ) -> None:
        self._gate = gate
        self._registry = registry
        self._store = store
        self._config = config
        self._clock = clock
        # None means the module's LEFT_ON_TARGETS/STANDBY_TARGETS, looked up
        # when they are needed rather than copied here, so a test that
        # rebinds them after construction still changes what is evaluated.
        # Either way _warn() and the gate check every target again.
        self._left_on_targets = left_on_targets
        self._standby_targets = standby_targets
        self._pending: dict[str, PendingCut] = {}
        self._active: dict[str, dict] = {}
        self._standby_since: dict[str, float] = {}
        # CLOCK time of the most recent observe(), or None if no telemetry
        # has ever arrived. One stamp for the whole of _active and
        # _standby_since, not one per appliance, because observe()
        # replaces _active wholesale from a single payload -- every entry
        # in it is exactly as old as this.
        self._observed_at: float | None = None
        # Liveness, surfaced on /api/health. A tick() that raises every
        # second logs a traceback from api.py's _rules_loop and is
        # otherwise invisible: the rules engine simply stops warning and
        # stops acting, and nothing an operator can query says so.
        # _tick_errors is cumulative and never reset, so an INTERMITTENT
        # failure still shows up after a later tick clears _last_error.
        self._ticks = 0
        self._tick_errors = 0
        self._last_tick_at: float | None = None
        self._last_error: str | None = None
        # Ruling C: once Task 5 wires observe() to the inbound telemetry
        # hook, it runs on paho's MQTT thread, while tick(), pending() and
        # cancel() run on the asyncio loop and FastAPI request threads.
        # _pending, _active and _standby_since are all read-modify-written
        # across that boundary. Mirrors gate.py's _awaiting_lock exactly:
        # held only long enough to touch these dicts, and released before
        # any call into Store or Gate - see the internals below for where
        # each of those releases happens and why.
        self._state_lock = threading.Lock()

    @property
    def config(self) -> RuleConfig:
        return self._config

    def config_targets(self, rule: str) -> tuple[str, ...]:
        if rule == "left_on":
            if self._left_on_targets is not None:
                return self._left_on_targets
            return LEFT_ON_TARGETS
        if self._standby_targets is not None:
            return self._standby_targets
        return STANDBY_TARGETS

    def observe(self, telemetry: dict) -> None:
        """Absorb one telemetry sample into the engine's tracked state.

        Ruling A - the one time base this whole module reasons in:
        `telemetry["ts"]` and each active item's `"since"` are BOTH
        telemetry time. `sim publish` (and every scenario run) builds
        payloads from a `start_ts` that defaults to a fixed literal over a
        year in the past (server/tests/test_end_to_end.py documents this
        at the top of that file), and a real device's clock has no reason
        to agree with either. `self._clock()` is a THIRD, independently
        injected time base - in production, wall-clock time; in tests, a
        fake that only moves when a test advances it.

        Comparing clock time against telemetry time directly -
        `self._clock() - item["since"]` - is not an on-duration. It is an
        offset of roughly a year, and both rules would fire on the very
        first telemetry sample, warning about everything immediately.

        The fix: derive the on-duration from the payload's OWN two
        numbers only - `telemetry["ts"] - item["since"]`, both telemetry
        time, so their difference is meaningful whatever the epoch is -
        then re-anchor that duration onto `self._clock()` and store THAT
        as `since`. From this method on, every duration this engine
        reasons about (`_active[...]["since"]`, `_standby_since`, and via
        `_warn`, `PendingCut.acts_at`) lives in exactly one time base: the
        injected clock's. `_evaluate()` never touches telemetry time at
        all, which is what makes that hold structurally rather than
        merely by coincidence of a test fixture where the two bases
        happen to agree.
        """
        clock_now = self._clock()
        active: dict[str, dict] = {}
        for item in telemetry["attribution"]["active"]:
            on_duration = telemetry["ts"] - item["since"]
            active[item["id"]] = {**item, "since": clock_now - on_duration}

        low, high = self._config.standby_band_w
        with self._state_lock:
            self._active = active
            # Stamped with the CLOCK, not with telemetry["ts"]: this is
            # "when did the server last hear anything", which is a fact
            # about this process, and _evaluate() compares it against the
            # same clock. telemetry["ts"] is a different time base
            # entirely (see this method's Ruling A note above) and would
            # make every observation look either a year stale or a year
            # early.
            self._observed_at = clock_now
            for appliance_id, item in active.items():
                if low <= item["w"] <= high:
                    # Anchored directly to the clock rather than derived
                    # from a telemetry-time duration: unlike left_on,
                    # nothing about "how long has this been in the
                    # standby band" is carried in the payload at all, so
                    # there is no telemetry-time duration to re-anchor -
                    # only a fresh clock-time stamp the first observation
                    # that finds an appliance in-band, kept via
                    # setdefault() until it leaves the band.
                    self._standby_since.setdefault(appliance_id, clock_now)
                else:
                    self._standby_since.pop(appliance_id, None)
            for gone in set(self._standby_since) - set(active):
                del self._standby_since[gone]

            # A pending cut for something that has since gone off is moot.
            for pending in list(self._pending.values()):
                if pending.appliance_id not in active:
                    del self._pending[pending.id]

    def tick(self) -> list[Result]:
        now = self._clock()
        try:
            self._gate.sweep_timeouts()
            # BEFORE _evaluate: state the engine can no longer substantiate
            # must stop being actionable in the same tick it goes stale,
            # not one tick later.
            self._abandon_stale_evidence(now)
            self._evaluate(now)
            results = self._act(now)
        except Exception as exc:
            # Recorded, then re-raised unchanged. api.py's _rules_loop
            # still catches and logs the traceback -- this only makes the
            # failure QUERYABLE, and swallowing it here would turn a loud
            # loop error into a silent one.
            with self._state_lock:
                self._tick_errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        with self._state_lock:
            self._ticks += 1
            self._last_tick_at = now
            self._last_error = None
        return results

    def health(self) -> dict:
        """What an operator can ask about the engine itself.

        Every duration is an AGE against the injected clock rather than a
        timestamp, so a reader does not have to know which epoch this
        process is using to tell whether the engine is alive.

        `observations_fresh` is False when nothing has ever been observed,
        not None: the engine having no actionable telemetry is the same
        operational fact whether the broker went away or never arrived,
        and a tri-state here would only invite a caller to treat one of
        them as "probably fine". The distinction is still available --
        `telemetry_age_s` is None in exactly the never-observed case.
        """
        now = self._clock()
        with self._state_lock:
            observed_at = self._observed_at
            last_tick_at = self._last_tick_at
            ticks = self._ticks
            errors = self._tick_errors
            last_error = self._last_error
            pending_cuts = len(self._pending)
        telemetry_age = None if observed_at is None else now - observed_at
        return {
            "ticks": ticks,
            "tick_errors": errors,
            "last_tick_age_s": None if last_tick_at is None else now - last_tick_at,
            "last_error": last_error,
            "telemetry_age_s": telemetry_age,
            "max_observation_age_s": MAX_OBSERVATION_AGE_S,
            "observations_fresh": (
                telemetry_age is not None and telemetry_age <= MAX_OBSERVATION_AGE_S
            ),
            "pending_cuts": pending_cuts,
        }

    def pending(self) -> list[PendingCut]:
        with self._state_lock:
            return list(self._pending.values())

    def cancel(self, pending_id: str) -> bool:
        # Ruling D: the write goes through Store's own locked method,
        # never a direct connection.execute()/.commit(). A direct write
        # here would run outside Store's write lock while the ingest
        # thread and the gate can be writing to the SAME connection
        # concurrently - see store.py's class docstring on why that lock
        # exists, and why close() is written to wait for it rather than
        # closing out from under an in-flight write.
        with self._state_lock:
            pending = self._pending.pop(pending_id, None)
        if pending is None:
            return False

        # Outside _state_lock, matching gate.py's own pattern throughout:
        # this is a Store call, and nothing here needs the lock held
        # while it runs.
        self._store.write_action(
            self._clock(), Actor.USER.value, pending.appliance_id, "OFF",
            pending.rule, Outcome.CANCELLED.value,
            "cancelled by the user during the grace period",
        )

        # Judgement call: cancelling does not touch _active or
        # _standby_since, so the condition that triggered the warning
        # (still on, or still drawing standby power) is still true, and
        # the very next _evaluate() will warn again and start a fresh
        # grace period. RuleConfig has no "snooze" field to suppress
        # that, and this deliberately does not invent one under this
        # task's interface: a user who cancels is making an active choice
        # to keep the appliance on RIGHT NOW, not a standing instruction
        # never to ask again, and a user who keeps cancelling is making
        # that same active choice repeatedly - this system should respect
        # it every time it is made, not just the first. The alternative -
        # suppressing re-warns after one cancel - would mean a single
        # cancel followed by genuinely forgetting the appliance (exactly
        # the failure left_on exists to catch) goes uncaught forever.
        # Immediate re-arming is the safer default of the two.
        return True

    # -- internals ---------------------------------------------------------

    def _abandon_stale_evidence(self, now: float) -> None:
        """Drop tracked state, and any pending cut resting on it, once the
        telemetry behind it is older than MAX_OBSERVATION_AGE_S.

        TWO things go, for one reason. The tracked state goes because
        `_evaluate()` would otherwise keep measuring a duration off an
        observation the server can no longer renew, and warn on it. An
        ALREADY-PENDING cut goes for the stronger version of the same
        reason: it is a countdown, shown live on the Control screen,
        asserting that this appliance is on RIGHT NOW -- and once the
        evidence has gone stale that assertion is one the server cannot
        make. Leaving the countdown running and cutting at the end of it
        would be acting on a claim about the present that nothing
        supports. Non-negotiable #2 rules that out even though the
        direction is always OFF and the gate's checks still hold.

        Logged as CANCELLED, with the age in `reason`. Not a new outcome:
        the spec's vocabulary is closed at eight, and no command was ever
        sent, which is exactly what CANCELLED means everywhere else here.
        Actor is `rule`, not `user` -- no one cancelled this; the system
        withdrew its own warning, and the log should not say a person did.
        The row matters: a pending cut that simply vanished from
        /api/pending would leave an evaluator with a warning they saw,
        then no warning and no record either way.

        Nothing has ever been observed => nothing to abandon: `_active`
        and `_pending` are both empty in that state (a pending cut can
        only come from a warn, and a warn only from an observation), so
        this returns rather than treating "never" as infinitely stale.
        """
        with self._state_lock:
            observed_at = self._observed_at
            if observed_at is None or now - observed_at <= MAX_OBSERVATION_AGE_S:
                return
            age = now - observed_at
            abandoned = list(self._pending.values())
            self._pending.clear()
            self._active.clear()
            self._standby_since.clear()

        # Outside _state_lock, matching cancel() and _act(): these are
        # Store writes, and Store takes its own lock.
        for pending in abandoned:
            log.warning(
                "%s for %s abandoned: no telemetry for %.0fs (limit %.0fs)",
                pending.rule, pending.appliance_id, age, MAX_OBSERVATION_AGE_S,
            )
            self._store.write_action(
                self._clock(), Actor.RULE.value, pending.appliance_id, "OFF",
                pending.rule, Outcome.CANCELLED.value,
                f"withdrawn: no telemetry for {age:.0f}s "
                f"(limit {MAX_OBSERVATION_AGE_S:.0f}s), so the server can no "
                "longer confirm this appliance is still on",
            )

    def _already_pending(self, appliance_id: str) -> bool:
        # Always called with _state_lock already held, from _warn().
        return any(p.appliance_id == appliance_id for p in self._pending.values())

    def _warn(self, appliance_id: str, rule: str, now: float) -> None:
        # Registry checks happen before _state_lock is ever taken, the
        # same way gate.py's send() resolves the registry before taking
        # _awaiting_lock: Registry.get()/is_pseudo() only ever read
        # Store, and Store's readers deliberately do not take its write
        # lock (see store.py's class docstring), so there is nothing here
        # for _state_lock to protect until _pending itself is touched.
        #
        # Never warn about something that could never be cut anyway: it
        # would be an alarm with no possible resolution. This is the
        # first of two independent, redundant checks that stand between
        # a misconfigured target list and a cut - the second is the
        # gate's own registry-backed checks in Gate.send(), reached from
        # _act() below. test_rules.py's rewritten protection tests
        # deliberately corrupt LEFT_ON_TARGETS to include a protected and
        # a pseudo appliance and prove BOTH layers hold independently.
        if self._registry.is_pseudo(appliance_id):
            return
        device = self._registry.get(appliance_id)
        if device is None or device.plug_device is None or device.protected:
            return

        with self._state_lock:
            if self._already_pending(appliance_id):
                return
            pending = PendingCut(
                id=str(uuid.uuid4()),
                appliance_id=appliance_id,
                rule=rule,
                warned_at=now,
                acts_at=now + self._config.grace_seconds,
            )
            self._pending[pending.id] = pending
        log.info("%s warned for %s; acts at %s", rule, appliance_id, pending.acts_at)

    def _evaluate(self, now: float) -> None:
        # Snapshot _active/_standby_since under the lock, then work off
        # the copies with the lock released. _warn() below takes
        # _state_lock itself, and holding it here across that call would
        # deadlock a plain (non-reentrant) Lock against its own thread -
        # matching gate.py's choice of Lock over RLock throughout, every
        # acquisition in this module is a single, non-nested critical
        # section rather than relying on reentrancy. A SHALLOW copy is
        # enough: observe() only ever replaces _active's entries wholesale
        # (a fresh dict built per appliance, see observe() above), never
        # mutating an existing one in place, so a shallow copy can never
        # observe a torn inner dict.
        with self._state_lock:
            active_snapshot = dict(self._active)
            standby_snapshot = dict(self._standby_since)

        for appliance_id in self.config_targets("left_on"):
            item = active_snapshot.get(appliance_id)
            if item is None:
                continue
            if now - item["since"] >= self._config.left_on_seconds:
                self._warn(appliance_id, "left_on", now)

        for appliance_id in self.config_targets("standby_draw"):
            began = standby_snapshot.get(appliance_id)
            if began is None:
                continue
            if now - began >= self._config.standby_seconds:
                self._warn(appliance_id, "standby_draw", now)

    def _act(self, now: float) -> list[Result]:
        with self._state_lock:
            ready = [p for p in self._pending.values() if now >= p.acts_at]
            for p in ready:
                del self._pending[p.id]
                # Judgement call: also drop the just-cut appliance from
                # _active/_standby_since here, not only from _pending.
                # observe() would do this on its own very next call once
                # the appliance's wattage actually drops - but that call
                # arrives on a different thread in production (the MQTT
                # ingest thread, vs. tick() on the rules loop) and nothing
                # guarantees it lands before the NEXT tick(). Without
                # this, a tick() that races ahead of the next observe()
                # would still see the pre-cut reading, decide the
                # condition still holds, and immediately re-warn for a
                # device that was just told to turn off - not unsafe
                # (sending OFF again is inert; the gate and the plug both
                # treat a repeat command as ordinary) but a confusing,
                # nagging re-warning of something already handled. This
                # closes that window without depending on which thread
                # wins the race. It does NOT suppress a legitimate
                # re-warn if the cut genuinely failed to take effect: the
                # next real observe() with fresh telemetry repopulates
                # both dicts from the payload as normal, exactly as if
                # this appliance had never been seen before.
                self._active.pop(p.appliance_id, None)
                self._standby_since.pop(p.appliance_id, None)

        # Outside _state_lock, matching gate.py's sweep_timeouts():
        # Gate.send() takes its own lock and does its own Store writes,
        # and nothing here needs _state_lock held while that runs.
        results: list[Result] = []
        for pending in ready:
            results.append(
                self._gate.send(
                    Command(pending.appliance_id, "OFF"), Actor.RULE, pending.rule
                )
            )
        return results
