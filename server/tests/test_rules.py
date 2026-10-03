import tokenize
from pathlib import Path

import pytest

from smartwatt_server.gate import Gate
from smartwatt_server.registry import Registry
from smartwatt_server import rules as rules_module
from smartwatt_server.rules import (
    DEMO_DEFAULTS,
    SHIPPED_DEFAULTS,
    PendingCut,
    RuleConfig,
    RulesEngine,
)


class SpyPublisher:
    def __init__(self):
        self.sent = []

    def __call__(self, topic, payload, authority=None):
        self.sent.append((topic, payload))
        return True


class Clock:
    """Injected so tests advance time rather than sleeping through it.
    A test suite that waits sixty seconds gets skipped, and a skipped
    safety test is worse than none."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def engine(store):
    registry = Registry(store)
    registry.seed_defaults()
    clock = Clock()
    publisher = SpyPublisher()
    gate = Gate(registry, store, publisher=publisher, clock=clock)
    config = RuleConfig(left_on_seconds=60.0, standby_band_w=(1.0, 12.0),
                        standby_seconds=120.0, grace_seconds=60.0, is_demo=True)
    engine = RulesEngine(gate, registry, store, config, clock=clock)
    engine.clock, engine.publisher = clock, publisher
    return engine


def telemetry(ts, active, total=None):
    return {
        "ts": ts,
        "electrical": {"p": total if total is not None else sum(a["w"] for a in active)},
        "attribution": {"active": active, "residual_w": 0.0, "floor_w": 6.0},
    }


def lamp_on(since):
    return [{"id": "incandescent_lamp", "w": 40.0, "since": since}]


# -- left_on ----------------------------------------------------------------

def test_left_on_does_not_fire_before_the_threshold(engine):
    engine.observe(telemetry(engine.clock.now, lamp_on(engine.clock.now)))
    engine.tick()
    assert engine.pending() == []


def test_left_on_warns_after_the_threshold(engine):
    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()
    assert len(engine.pending()) == 1
    assert engine.pending()[0].rule == "left_on"


def test_warning_sends_nothing_yet(engine):
    """US41: two-stage without exception. The system never acts silently."""
    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()
    assert engine.publisher.sent == []


def test_the_cut_lands_only_after_the_grace_period(engine):
    """Telemetry is re-observed at every step, as it is in production at
    1 Hz -- the grace period (60 s) is longer than
    MAX_OBSERVATION_AGE_S (30 s) by design, so a countdown running on a
    SINGLE stale observation is withdrawn rather than acted on. That is a
    different property, and
    test_a_pending_cut_is_withdrawn_and_logged_when_its_evidence_goes_stale
    below is what covers it. This test is about the grace period alone.
    """
    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()

    engine.clock.advance(30)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()
    assert engine.publisher.sent == []

    engine.clock.advance(31)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()
    assert engine.publisher.sent == [("cmnd/plug_lamp/POWER", "OFF")]


def test_cancelling_during_grace_sends_nothing(engine, store):
    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()

    assert engine.cancel(engine.pending()[0].id) is True
    engine.clock.advance(120)
    engine.tick()

    assert engine.publisher.sent == []
    outcomes = [r["outcome"] for r in store.connection.execute("SELECT outcome FROM actions")]
    assert "CANCELLED" in outcomes


def test_cancelling_an_unknown_pending_is_false(engine):
    assert engine.cancel("nope") is False


def test_left_on_targets_the_incandescent_lamp(engine):
    """Retargeted from the draft's soldering iron. The original is not in
    the trained set, and the only heating load that IS - the kettle - sits
    in the no-auto-on set and self-terminates at boil, so the rule could
    never fire honestly."""
    assert "incandescent_lamp" in engine.config_targets("left_on")


def test_left_on_never_targets_a_heating_load(engine):
    assert "kettle" not in engine.config_targets("left_on")


def test_a_pending_cut_clears_if_the_appliance_goes_off(engine):
    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()
    assert engine.pending()

    engine.observe(telemetry(engine.clock.now, []))
    engine.tick()
    assert engine.pending() == []


# -- standby_draw -------------------------------------------------------
#
# The brief's own loop bound here was 200 (seconds). _standby_since is
# first set at the loop's first tick (clock t0+10); with this fixture's
# standby_seconds=120, the warn condition first holds at t0+130; the
# resulting PendingCut's grace_seconds=60 grace period then elapses at
# t0+190 - still inside a 200 s loop, so by the loop's last iteration
# _act() has already turned that pending into a sent cut.
#
# Whether `engine.pending()` still shows a standby_draw entry after that
# is NOT a property "a correct implementation" would share in general -
# it depends entirely on what _act() does to _active/_standby_since when
# it cuts something. THIS engine's _act() (rules.py:324-325, judgement
# call 1 in task-4-report.md) pops the just-cut appliance out of both
# dicts, so by t0+200 there is nothing stale left to re-fire and
# `pending()` comes back empty: the brief's 200-bound test fails here,
# specifically because of that choice.
#
# Proven by counterfactual, not asserted: reverting ONLY those two
# pop() calls (i.e. the brief's own original _act(), which never touches
# _active/_standby_since) leaves led_bulb's stale _standby_since entry
# in place after the cut. The loop's last tick (t0+200) re-evaluates it,
# finds 200-10=190 >= 120 still true, and - _already_pending now False,
# the first pending having just been consumed by the cut - fires a BRAND
# NEW PendingCut at that very last iteration. Under the brief's own
# _act(), the 200-bound test would therefore have PASSED - for a reason
# unrelated to what it is named for: a second, coincidental warn
# triggered by staleness, not the first one the test means to observe.
#
# 150 sidesteps the question entirely: it clears the warn threshold (130)
# with the same margin as before but stops well short of the act deadline
# (190), so the test observes exactly the warn it is named for and
# nothing downstream of it, regardless of what _act() does to these dicts
# on a cut - which is not what this test is about. Both loops below share
# the bound for symmetry - the "above the band" test never creates a
# pending at all, so its own bound is not load-bearing, but a mismatched
# pair would be a needless puzzle for whoever reads them next.

def test_standby_warns_after_a_sustained_in_band_draw(engine):
    since = engine.clock.now
    for step in range(0, 150, 10):
        engine.clock.advance(10)
        engine.observe(telemetry(
            engine.clock.now,
            [{"id": "led_bulb", "w": 5.0, "since": since}],
        ))
        engine.tick()
    assert any(p.rule == "standby_draw" for p in engine.pending())


def test_standby_does_not_fire_above_the_band(engine):
    since = engine.clock.now
    for step in range(0, 150, 10):
        engine.clock.advance(10)
        engine.observe(telemetry(
            engine.clock.now,
            [{"id": "led_bulb", "w": 40.0, "since": since}],
        ))
        engine.tick()
    assert not any(p.rule == "standby_draw" for p in engine.pending())


# -- protection holds through the rules --------------------------------
#
# The brief's originals for both tests below put the protected/pseudo
# appliance nowhere near LEFT_ON_TARGETS/STANDBY_TARGETS, so the rule
# never even considered them - _warn() was never called, `_gate.send()`
# was never called, and "nothing published" held trivially. Deleting
# every protective check in _warn would have left both tests green: they
# asserted the target lists were correctly built, not that protection
# holds.
#
# These versions corrupt the target list itself (the same technique
# Task 3's mutation test uses on the gate's own registry lookup) so the
# rule is actually asked to warn about a protected/pseudo appliance, in
# two steps:
#   1. Run the engine's normal observe()/tick() flow with the corrupted
#      target list. This proves _warn()'s own guard refuses to create a
#      PendingCut - if it didn't, `pending()` would show one.
#   2. Insert a PendingCut directly, past _warn() entirely - standing in
#      for what a hole in that guard would look like, the same way
#      test_gate.py's test_protected_guard_does_not_rely_on_command_
#      validation_alone reaches past Command's constructor with
#      object.__setattr__ rather than assuming the earlier guard is the
#      only thing stopping a cut. This proves _act()'s call into the
#      gate refuses independently and LEAVES A RECORDED REFUSAL, even
#      with _warn()'s guard bypassed entirely.
# Either guard alone is enough to stop the cut; both are checked, so
# deleting one alone still leaves the appliance uncuttable and evidenced.

def test_a_rule_cannot_cut_the_protected_load(engine, store, monkeypatch):
    monkeypatch.setattr(rules_module, "LEFT_ON_TARGETS", ("laptop_charger",))

    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(
        engine.clock.now,
        [{"id": "laptop_charger", "w": 65.0, "since": since}],
    ))
    engine.tick()
    # Layer 1: _warn()'s own guard. A protected appliance in the target
    # list must never even become a pending cut - if it did, the user
    # would see a "cut coming" warning for something that can never
    # actually be cut, which is its own kind of dishonesty.
    assert engine.pending() == []
    assert engine.publisher.sent == []

    # Layer 2: the gate itself, reached past _warn() on purpose.
    phantom = PendingCut(id="phantom", appliance_id="laptop_charger",
                          rule="left_on", warned_at=engine.clock.now,
                          acts_at=engine.clock.now)
    engine._pending[phantom.id] = phantom  # noqa: SLF001 - simulate a bypassed guard
    engine.tick()
    assert engine.publisher.sent == []
    outcomes = [r["outcome"] for r in store.connection.execute("SELECT outcome FROM actions")]
    assert "REFUSED_PROTECTED" in outcomes


def test_a_rule_never_targets_a_pseudo_appliance(engine, store, monkeypatch):
    monkeypatch.setattr(rules_module, "LEFT_ON_TARGETS", ("unknown_1",))

    since = engine.clock.now
    engine.clock.advance(200)
    engine.observe(telemetry(
        engine.clock.now,
        [{"id": "unknown_1", "w": 305.0, "since": since}],
    ))
    engine.tick()
    assert engine.pending() == []
    assert engine.publisher.sent == []

    phantom = PendingCut(id="phantom", appliance_id="unknown_1",
                          rule="left_on", warned_at=engine.clock.now,
                          acts_at=engine.clock.now)
    engine._pending[phantom.id] = phantom  # noqa: SLF001 - simulate a bypassed guard
    engine.tick()
    assert engine.publisher.sent == []
    outcomes = [r["outcome"] for r in store.connection.execute("SELECT outcome FROM actions")]
    assert "REFUSED_NOT_CONTROLLABLE" in outcomes


# -- stale telemetry (Ruling R31) ------------------------------------------
#
# observe() is the only writer of _active, and _evaluate() measures against
# the clock. Before MAX_OBSERVATION_AGE_S, ONE sample claiming the lamp had
# been on for 10 s followed by total silence still warned, waited out the
# grace period and published a real cut thousands of seconds later. The
# three tests below are the two halves of the fix plus the boundary that
# keeps it from being a blanket "ignore everything".

def test_the_bound_stays_below_every_grace_period():
    """The freshness bound is only decidable for a PENDING cut while it is
    shorter than the grace period: a warning raised on fresh evidence that
    then stops being renewed must be withdrawn (at the bound) before it
    can act (at the end of the grace period).

    MAX_OBSERVATION_AGE_S's own comment claims exactly that, so the
    relationship is pinned rather than left to a reader to re-derive. A
    future RuleConfig with a shorter grace period fails here instead of
    quietly reopening the gap.
    """
    for config in (SHIPPED_DEFAULTS, DEMO_DEFAULTS):
        assert rules_module.MAX_OBSERVATION_AGE_S < config.grace_seconds, config


def test_one_sample_then_silence_never_becomes_a_cut(engine):
    """The measured failure, reproduced and now refused.

    Deliberately mirrors the reviewer's experiment: a single observation,
    then nothing but ticks. If the freshness bound is removed, the final
    assertion fails with a real ("cmnd/plug_lamp/POWER", "OFF") on the
    publisher -- checked by mutation, not assumed.
    """
    engine.observe(telemetry(engine.clock.now, lamp_on(engine.clock.now - 10)))

    for _ in range(600):  # 6,000 s of ticks, and no telemetry ever again
        engine.clock.advance(10)
        engine.tick()

    assert engine.publisher.sent == [], (
        "the engine cut an appliance it had not heard from in over an hour"
    )
    assert engine.pending() == []


def test_a_pending_cut_is_withdrawn_and_logged_when_its_evidence_goes_stale(
    engine, store,
):
    """A countdown the server can no longer substantiate is withdrawn --
    and leaves a row saying so.

    The warning is real when it is raised (fresh telemetry, threshold
    genuinely crossed); what changes underneath it is that the telemetry
    stops. Both halves are asserted: the pending disappears AND the log
    explains why, because a countdown that silently vanished would leave
    an evaluator who saw it with no record either way.
    """
    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(engine.clock.now, lamp_on(since)))
    engine.tick()
    assert len(engine.pending()) == 1

    # Past the freshness bound, but the grace period (60 s) has NOT
    # elapsed at the point staleness bites -- otherwise the cut would
    # simply have happened and this would prove nothing about staleness.
    engine.clock.advance(rules_module.MAX_OBSERVATION_AGE_S + 1)
    engine.tick()

    assert engine.pending() == []
    assert engine.publisher.sent == []
    rows = [dict(r) for r in store.connection.execute(
        "SELECT * FROM actions WHERE outcome = 'CANCELLED'"
    )]
    assert len(rows) == 1
    assert rows[0]["device"] == "incandescent_lamp"
    assert rows[0]["rule"] == "left_on"
    # `rule`, not `user`: nobody cancelled this. The system withdrew its
    # own warning, and the log must not claim a person did.
    assert rows[0]["actor"] == "rule"
    assert "no telemetry" in rows[0]["reason"]


def test_telemetry_that_keeps_arriving_still_cuts(engine):
    """The bound must not be a blanket refusal to act.

    Same elapsed time as the silence test above, but telemetry keeps
    arriving -- so the warn, the grace period and the cut all still
    happen. Without this, deleting the entire rules engine, or setting
    MAX_OBSERVATION_AGE_S to 0, would pass the two tests above.

    More than one cut lands over 300 s, and that is correct rather than
    something to assert away: each observation reports the lamp STILL on
    (`since` never moves), so after _act() clears its tracking the next
    fresh sample re-establishes the same condition and the rule warns
    again. That is the honest difference from the silence case -- there,
    nothing renews the claim; here, every 10 s does.
    """
    since = engine.clock.now
    for _ in range(30):
        engine.clock.advance(10)
        engine.observe(telemetry(engine.clock.now, lamp_on(since)))
        engine.tick()

    assert engine.publisher.sent, "sustained telemetry must still produce a cut"
    assert set(engine.publisher.sent) == {("cmnd/plug_lamp/POWER", "OFF")}


# -- engine liveness on /api/health (Ruling R31) ---------------------------

def test_health_reports_ticks_and_observation_freshness(engine):
    fresh_start = engine.health()
    assert fresh_start["ticks"] == 0
    assert fresh_start["telemetry_age_s"] is None
    assert fresh_start["last_tick_age_s"] is None
    # Never observed is not "fresh": the engine has nothing it can act on.
    assert fresh_start["observations_fresh"] is False

    engine.observe(telemetry(engine.clock.now, lamp_on(engine.clock.now)))
    engine.tick()
    after = engine.health()
    assert after["ticks"] == 1
    assert after["tick_errors"] == 0
    assert after["last_error"] is None
    assert after["telemetry_age_s"] == pytest.approx(0.0)
    assert after["observations_fresh"] is True

    engine.clock.advance(rules_module.MAX_OBSERVATION_AGE_S + 1)
    engine.tick()
    stale = engine.health()
    assert stale["ticks"] == 2
    assert stale["telemetry_age_s"] == pytest.approx(
        rules_module.MAX_OBSERVATION_AGE_S + 1
    )
    assert stale["observations_fresh"] is False
    # The clock advanced, but a tick just ran: liveness and freshness are
    # separate facts and must not collapse into one another.
    assert stale["last_tick_age_s"] == pytest.approx(0.0)


def test_a_failing_tick_is_recorded_rather_than_only_logged(engine, monkeypatch):
    """A tick() that raises every second logs a traceback from api.py's
    _rules_loop and is otherwise invisible: the engine stops warning and
    stops acting, and nothing an operator can query says so.

    The failure must still PROPAGATE -- _rules_loop's own logging is what
    produces the traceback, and swallowing it here would trade a loud
    failure for a silent one. So both are asserted: it raises, and it is
    counted.
    """
    def explode():
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(engine._gate, "sweep_timeouts", explode)  # noqa: SLF001

    with pytest.raises(RuntimeError):
        engine.tick()

    failing = engine.health()
    assert failing["tick_errors"] == 1
    assert failing["ticks"] == 0
    assert "sweep exploded" in failing["last_error"]
    assert failing["last_tick_age_s"] is None, (
        "a tick that raised must not count as a completed tick"
    )

    # Recovery clears last_error but never the cumulative count, so an
    # INTERMITTENT failure does not become invisible the moment one tick
    # succeeds.
    monkeypatch.undo()
    engine.tick()
    recovered = engine.health()
    assert recovered["last_error"] is None
    assert recovered["ticks"] == 1
    assert recovered["tick_errors"] == 1


# -- configuration --------------------------------------------------------

def test_shipped_default_is_realistic():
    assert SHIPPED_DEFAULTS.left_on_seconds >= 3600
    assert SHIPPED_DEFAULTS.is_demo is False


def test_demo_default_is_short_and_says_so(engine):
    """US50, honestly: a demo threshold presented as a shipped default
    would be a small dishonesty of exactly the kind ruled out."""
    assert DEMO_DEFAULTS.left_on_seconds <= 120
    assert DEMO_DEFAULTS.is_demo is True


def test_occupancy_appears_only_as_prose_never_as_a_working_identifier():
    """Renamed from the brief's test_occupancy_is_not_referenced_anywhere.

    The draft's idle rule claimed to act on a signal that does not exist.
    Its honest form IS left_on, a timer, which is what this implements -
    and the module docstring says so. But the brief's own version of this
    test -
        assert "occupancy" not in text.lower() or "not built" in text.lower()
    - can never fail. The required module docstring's own "OCCUPANCY
    SENSING IS NOT BUILT" makes the right-hand side of that `or`
    permanently True, so nothing past it is ever exercised - someone
    could add a working `def read_occupancy_sensor():` tomorrow and this
    assertion would stay green forever.

    This version tokenises the module instead of pattern-matching its raw
    text, and only fails on "occupancy" inside a NAME token - an actual
    Python identifier: a function, class, variable, parameter or
    attribute reference. Comments and every string literal (the required
    docstring included) are exempt, which is exactly "prose disavowing
    it" - prose lives in STRING and COMMENT tokens, never in a NAME. A
    real occupancy mechanism cannot be built without naming it somewhere
    - a sensor class, a reading function, a new target list, a condition
    variable - so this catches the failure mode the brief warns about
    (real code) without also forbidding the disavowal the module is
    required to carry (prose).
    """
    path = (Path(__file__).resolve().parents[1]
            / "smartwatt_server" / "rules.py")

    with open(path, "rb") as f:
        tokens = list(tokenize.tokenize(f.readline))

    offenders = [
        f"line {tok.start[0]}: identifier {tok.string!r}"
        for tok in tokens
        if tok.type == tokenize.NAME and "occupancy" in tok.string.lower()
    ]
    assert offenders == [], (
        "occupancy sensing is not built; rules.py may disavow it in prose "
        "(comments, docstrings) but must never name it as working code:\n"
        + "\n".join(offenders)
    )

    # The disavowal itself must still be there - this module is required
    # to say, in prose, that occupancy sensing is not built. Without this
    # second assertion, deleting the whole module docstring would still
    # pass the check above (no NAME tokens mention it either), which
    # would silently drop the disavowal this test exists to enforce.
    assert "occupancy" in path.read_text().lower()


# -- targets from the household's appliance list -----------------------------

def _engine_with_targets(store, left_on, standby):
    registry = Registry(store)
    registry.seed_defaults()
    clock = Clock()
    publisher = SpyPublisher()
    gate = Gate(registry, store, publisher=publisher, clock=clock)
    config = RuleConfig(left_on_seconds=60.0, standby_band_w=(1.0, 12.0),
                        standby_seconds=120.0, grace_seconds=60.0, is_demo=True)
    engine = RulesEngine(gate, registry, store, config, clock=clock,
                         left_on_targets=left_on, standby_targets=standby)
    engine.clock, engine.publisher = clock, publisher
    return engine


def test_given_targets_replace_the_example_households(store):
    """appliances.toml's left_on_rule/standby_rule flags reach the engine as
    these arguments. The example lamp is no longer watched; the LED is."""
    engine = _engine_with_targets(store, left_on=("led_bulb",), standby=())
    assert engine.config_targets("left_on") == ("led_bulb",)
    assert engine.config_targets("standby_draw") == ()

    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(engine.clock.now, [
        {"id": "incandescent_lamp", "w": 40.0, "since": since},
        {"id": "led_bulb", "w": 9.0, "since": since},
    ]))
    engine.tick()
    assert [(p.appliance_id, p.rule) for p in engine.pending()] == [
        ("led_bulb", "left_on")
    ]


def test_a_given_protected_target_is_still_never_cut(store):
    """The loader refuses a protected appliance with a rule, but the engine
    does not rely on that: both of its own layers still hold."""
    engine = _engine_with_targets(store, left_on=("laptop_charger",), standby=())
    since = engine.clock.now
    engine.clock.advance(120)
    engine.observe(telemetry(
        engine.clock.now, [{"id": "laptop_charger", "w": 65.0, "since": since}]
    ))
    engine.tick()
    engine.clock.advance(120)
    engine.tick()
    assert engine.pending() == []
    assert engine.publisher.sent == []
