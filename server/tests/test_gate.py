import threading

import pytest

from smartwatt_server.gate import Actor, Command, Gate, Outcome
from smartwatt_server.registry import Registry


class SpyPublisher:
    """Stands in for the private MQTT client and records what escaped."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.authorities: list[object] = []

    def __call__(self, topic: str, payload: str, authority=None) -> bool:
        # Third parameter: Gate presents its CommandAuthority on every
        # publish (gate.py). A spy that could not receive it would be
        # standing in for a publisher the real gate cannot call.
        self.sent.append((topic, payload))
        self.authorities.append(authority)
        return True


def _exploding_publisher(topic: str, payload: str, authority=None) -> bool:
    """Stands in for a publisher that raises -- broker down, client not
    yet connected -- rather than returning False."""
    raise ConnectionError("broker unreachable")


@pytest.fixture
def gate(store):
    registry = Registry(store)
    registry.seed_defaults()
    return Gate(registry, store, publisher=SpyPublisher())


def _sent(gate) -> list[tuple[str, str]]:
    return gate._publisher.sent  # noqa: SLF001 - test needs the spy


def test_ordinary_off_is_sent(gate):
    result = gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    assert result.outcome is Outcome.SENT
    assert _sent(gate) == [("cmnd/plug_lamp/POWER", "OFF")]


def test_ordinary_on_is_sent(gate):
    assert gate.send(Command("incandescent_lamp", "ON"), Actor.USER).outcome is Outcome.SENT


# -- the protected guarantee ------------------------------------------------

def test_rule_cannot_cut_a_protected_load(gate):
    result = gate.send(Command("laptop_charger", "OFF"), Actor.RULE, "left_on")
    assert result.outcome is Outcome.REFUSED_PROTECTED
    assert _sent(gate) == []


def test_user_cannot_cut_a_protected_load_either(gate):
    """US46: the guarantee holds regardless of who issues the command.
    A guarantee that holds only against one source is not a guarantee."""
    result = gate.send(Command("laptop_charger", "OFF"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_PROTECTED
    assert _sent(gate) == []


def test_a_protected_load_may_still_be_energised(gate):
    """Protection is one-directional: it prevents cutting, not switching on."""
    assert gate.send(Command("laptop_charger", "ON"), Actor.USER).outcome is Outcome.SENT


# -- heating is one-way -----------------------------------------------------

def test_heating_load_may_be_de_energised(gate):
    assert gate.send(Command("kettle", "OFF"), Actor.RULE, "left_on").outcome is Outcome.SENT


def test_heating_load_may_never_be_energised_by_a_rule(gate):
    result = gate.send(Command("kettle", "ON"), Actor.RULE, "some_rule")
    assert result.outcome is Outcome.REFUSED_HEATING_ONE_WAY
    assert _sent(gate) == []


def test_heating_load_may_never_be_energised_by_a_user_either(gate):
    """Automatic re-energising of an unattended heater is the one
    automation that can start a fire. The gate refuses it structurally."""
    result = gate.send(Command("kettle", "ON"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_HEATING_ONE_WAY
    assert _sent(gate) == []


# -- the other two checks ---------------------------------------------------

def test_unregistered_device_is_refused(gate):
    result = gate.send(Command("nonexistent", "OFF"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_UNREGISTERED
    assert _sent(gate) == []


def test_appliance_without_a_plug_is_refused(gate):
    result = gate.send(Command("desk_fan", "OFF"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_UNREGISTERED
    assert _sent(gate) == []


@pytest.mark.parametrize("pseudo", ["__residual__", "unknown_1", "unknown_42"])
def test_pseudo_appliances_are_not_controllable(gate, pseudo):
    result = gate.send(Command(pseudo, "OFF"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_NOT_CONTROLLABLE
    assert _sent(gate) == []


def test_a_pseudo_appliance_is_refused_even_with_a_registered_plug(gate, store):
    """The refusal does not rest on the registry not holding a row.

    Nothing refuses a pseudo row: `seed_defaults()` merely never writes
    one, and `upsert_appliance` would take `unknown_3` as readily as the
    kettle. What makes these ids uncontrollable is check 1 in
    `Gate.send()`, which runs BEFORE the registry is consulted at all --
    so writing the row does not open a path.

    Paired with the parametrised test above, this pins both properties:
    that one covers a pseudo id with NO row (which would come back
    REFUSED_UNREGISTERED if the pseudo check were moved below the
    registry lookup), and this one covers a pseudo id WITH a plugged row
    (which would come back SENT if the pseudo check were removed).
    """
    store.upsert_appliance(
        appliance_id="unknown_3", display_name="Unknown load 3",
        protected=False, heating=False, plug_device="plug_unknown_3",
    )
    assert gate._registry.get("unknown_3").plug_device == "plug_unknown_3"  # noqa: SLF001

    result = gate.send(Command("unknown_3", "OFF"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_NOT_CONTROLLABLE
    assert _sent(gate) == []


# -- logging ----------------------------------------------------------------

def test_a_sent_command_is_logged(gate, store):
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    row = store.connection.execute(
        "SELECT * FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "SENT"
    assert row["rule"] == "left_on"
    assert row["actor"] == "rule"


def test_every_refusal_is_logged(gate, store):
    """US48: the safety gate leaves evidence it worked."""
    gate.send(Command("laptop_charger", "OFF"), Actor.USER)
    gate.send(Command("kettle", "ON"), Actor.USER)
    gate.send(Command("nonexistent", "OFF"), Actor.USER)
    gate.send(Command("__residual__", "OFF"), Actor.USER)

    outcomes = [
        row["outcome"]
        for row in store.connection.execute("SELECT outcome FROM actions")
    ]
    assert set(outcomes) == {
        "REFUSED_PROTECTED", "REFUSED_HEATING_ONE_WAY",
        "REFUSED_UNREGISTERED", "REFUSED_NOT_CONTROLLABLE",
    }


def test_a_refusal_carries_a_reason(gate, store):
    gate.send(Command("laptop_charger", "OFF"), Actor.USER)
    row = store.connection.execute(
        "SELECT reason FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["reason"]


# -- confirmation -----------------------------------------------------------

def test_confirmation_upgrades_the_outcome(gate, store):
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    gate.confirm("plug_lamp", "OFF")
    row = store.connection.execute(
        "SELECT outcome FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "CONFIRMED"


def test_an_unexpected_confirmation_is_ignored(gate, store):
    gate.confirm("plug_lamp", "OFF")
    count = store.connection.execute(
        "SELECT COUNT(*) FROM actions"
    ).fetchone()[0]
    assert count == 0


def test_no_confirmation_within_the_timeout_is_logged_as_timeout(store):
    now = [1000.0]
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=SpyPublisher(), clock=lambda: now[0])
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    now[0] += 30.0
    gate.sweep_timeouts()
    row = store.connection.execute(
        "SELECT outcome FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "TIMEOUT"


def test_a_confirmation_that_contradicts_the_command_does_not_upgrade(store):
    """Ruling B: `confirm()` must actually use `state`. A plug replying
    with the OPPOSITE of what was commanded is not a confirmation - it is
    an anomaly, and logging it CONFIRMED would be an assumption of success
    that never happened. The row must stay SENT so sweep_timeouts() can
    still catch it: if the mismatch silently dropped the outstanding
    command instead of leaving it in place, this row would stay SENT
    forever rather than eventually timing out."""
    now = [1000.0]
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=SpyPublisher(), clock=lambda: now[0])
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")

    gate.confirm("plug_lamp", "ON")  # the opposite of what was commanded
    row = store.connection.execute(
        "SELECT outcome FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "SENT"

    now[0] += 30.0
    gate.sweep_timeouts()
    row = store.connection.execute(
        "SELECT outcome FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "TIMEOUT"


# ============================================================================
# Fix round: task review found the guards failed open on anything but an
# exact-case "ON"/"OFF" string, the publisher's own success signal was
# discarded, _awaiting was unlocked, and a superseded command's row could
# never be settled. The tests below cover each finding.
# ============================================================================

# -- Finding 1(a)/(c): Command validates and normalises its own value -------

@pytest.mark.parametrize("appliance_id", ["laptop_charger", "kettle"])
@pytest.mark.parametrize("bad", ["toggle", "TOGGLE", "0", "1", "2", "", "   "])
def test_command_rejects_anything_that_is_not_on_or_off(appliance_id, bad):
    """Tasmota's POWER command accepts TOGGLE and 0/1/2 as well as
    ON/OFF and is case-insensitive; Command only ever represents an
    unambiguous energise/de-energise, so anything else must not even be
    constructible - not just refused later by a guard that could be
    weakened or bypassed."""
    with pytest.raises(ValueError):
        Command(appliance_id, bad)


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("on", "ON"), ("ON", "ON"), (" On ", "ON"), ("oN", "ON"),
        ("off", "OFF"), ("OFF", "OFF"), ("\toff\n", "OFF"), ("Off", "OFF"),
    ],
)
def test_command_normalises_case_and_surrounding_whitespace(raw, canonical):
    assert Command("incandescent_lamp", raw).command == canonical


def test_a_lowercase_off_still_cannot_cut_a_protected_load(gate):
    """The exact attack the finding named: `cmd.command == "OFF"` is a
    case-sensitive equality, so `Command(id, "off")` used to pass the
    protected check, pass the heating check, and publish `off` verbatim
    to a case-insensitive Tasmota POWER topic. Normalising in Command's
    constructor closes it: by the time the guard runs, "off" is "OFF"."""
    result = gate.send(Command("laptop_charger", "off"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_PROTECTED
    assert _sent(gate) == []


def test_a_lowercase_on_still_cannot_energise_a_heating_load(gate):
    """The mirror hole: a mis-cased "on" re-energising the kettle."""
    result = gate.send(Command("kettle", "on"), Actor.USER)
    assert result.outcome is Outcome.REFUSED_HEATING_ONE_WAY
    assert _sent(gate) == []


def test_protected_guard_does_not_rely_on_command_validation_alone(gate):
    """Finding 1(b): the guard is written `!= "ON"`, not `== "OFF"`, on
    purpose, so the protected guarantee does not rest solely on Command's
    constructor. Force an uncanonicalised value into an already-built
    Command - standing in for what a hole in that validation would look
    like - past the normal API (a frozen dataclass blocks plain attribute
    assignment, which is the point: this requires deliberately reaching
    past the safeguard, not something that happens by accident)."""
    cmd = Command("laptop_charger", "ON")
    object.__setattr__(cmd, "command", "off")
    result = gate.send(cmd, Actor.USER)
    assert result.outcome is Outcome.REFUSED_PROTECTED
    assert _sent(gate) == []


def test_heating_guard_does_not_rely_on_command_validation_alone(gate):
    cmd = Command("kettle", "OFF")
    object.__setattr__(cmd, "command", "on")
    result = gate.send(cmd, Actor.USER)
    assert result.outcome is Outcome.REFUSED_HEATING_ONE_WAY
    assert _sent(gate) == []


# -- Finding 1(b): confirm() normalises `state` through the same helper -----

def test_confirm_accepts_a_lowercase_state(store):
    now = [1000.0]
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=SpyPublisher(), clock=lambda: now[0])
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    gate.confirm("plug_lamp", "off")
    row = store.connection.execute(
        "SELECT outcome FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "CONFIRMED"


def test_confirm_accepts_bytes_as_paho_delivers_them(store):
    """ingest.py hands a stat/ payload to on_message as raw bytes
    (`m.payload`); under plain string comparison `"OFF" != b"OFF"` is
    always true, which used to time out every real confirmation
    regardless of what the plug actually reported."""
    now = [1000.0]
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=SpyPublisher(), clock=lambda: now[0])
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    gate.confirm("plug_lamp", b"OFF")
    row = store.connection.execute(
        "SELECT outcome FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "CONFIRMED"


def test_confirm_treats_an_unrecognised_state_as_a_non_confirmation(store):
    """An unrecognised or undecodable reply must not upgrade the row and,
    just as importantly, must not raise - a crash here runs on whichever
    thread the MQTT client calls back on and would take it down."""
    now = [1000.0]
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=SpyPublisher(), clock=lambda: now[0])
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")

    gate.confirm("plug_lamp", "TOGGLE")
    gate.confirm("plug_lamp", b"\xff\xfe not valid utf-8")

    row = store.connection.execute(
        "SELECT outcome FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "SENT"


# -- Finding 2: the publisher's own success signal is no longer discarded --

def test_a_publisher_reporting_false_settles_the_row_as_timeout(store):
    """A publisher returning False - broker down, client not connected -
    must not leave the ledger claiming SENT for a command that never left
    the process. TIMEOUT is reused rather than adding a ninth outcome:
    it is honest about exactly what is known ("unconfirmed"), and the
    failure itself is preserved in `reason`."""
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=lambda topic, payload: False)

    result = gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    assert result.outcome is Outcome.TIMEOUT
    assert result.reason

    row = store.connection.execute(
        "SELECT outcome, reason FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "TIMEOUT"
    assert row["reason"]


def test_a_raising_publisher_still_leaves_evidence(store):
    """Before this fix, the row was written AFTER the publish call, so a
    raising publisher escaped before _log ever ran and produced ZERO rows
    in `actions` - a total absence of evidence in the one module whose
    entire purpose is leaving it."""
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=_exploding_publisher)

    result = gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    assert result.outcome is Outcome.TIMEOUT

    rows = store.connection.execute("SELECT outcome, reason FROM actions").fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "TIMEOUT"
    assert rows[0]["reason"]


def test_a_failed_publish_does_not_register_an_awaiting_confirmation(store):
    """If the command never left the process, a later stat/ reply for
    that plug must not find a phantom outstanding command to match
    against."""
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=lambda topic, payload: False)
    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    assert gate._awaiting == {}  # noqa: SLF001 - test needs internal state


# -- Finding 3: _awaiting is guarded by a lock -------------------------------

def test_confirm_takes_the_awaiting_lock(gate):
    """confirm() and sweep_timeouts() both read-modify-write _awaiting,
    from different threads in the real system (a stat/ reply on the MQTT
    thread, a sweep on the rules loop). Prove the lock meant to prevent
    that race is actually engaged, the same way test_store.py proves
    Store.close() waits on a held lock, rather than relying on real
    thread-scheduling luck to catch the race directly."""
    released = threading.Event()
    with gate._awaiting_lock:  # noqa: SLF001 - test needs to prove the lock is real
        worker = threading.Thread(
            target=lambda: (gate.confirm("plug_lamp", "OFF"), released.set())
        )
        worker.start()
        assert not released.wait(0.3), "confirm() ran while _awaiting_lock was held"

    assert released.wait(5.0), "confirm() never completed after the lock was freed"
    worker.join(timeout=5.0)


def test_sweep_timeouts_takes_the_awaiting_lock(gate):
    released = threading.Event()
    with gate._awaiting_lock:  # noqa: SLF001 - test needs to prove the lock is real
        worker = threading.Thread(
            target=lambda: (gate.sweep_timeouts(), released.set())
        )
        worker.start()
        assert not released.wait(0.3), "sweep_timeouts() ran while _awaiting_lock was held"

    assert released.wait(5.0), "sweep_timeouts() never completed after the lock was freed"
    worker.join(timeout=5.0)


# -- Finding 4: a superseded command's row is settled, not stranded -------

def test_a_superseded_commands_row_is_settled_the_instant_it_is_displaced(store):
    """_awaiting holds exactly one outstanding command per plug. A second
    send() to the same plug before the first confirms or times out used to
    silently overwrite the first entry's tracking, stranding its `actions`
    row at SENT forever - never confirmed, never timed out, since
    sweep_timeouts() only ever iterates what is CURRENTLY in _awaiting, and
    the first entry is gone the instant the second lands. send() now
    settles the displaced row to TIMEOUT itself, at the moment of
    supersession - no sweep, no clock advance, required to see it."""
    now = [1000.0]
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=SpyPublisher(), clock=lambda: now[0])

    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    first_row = store.connection.execute(
        "SELECT rowid FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()["rowid"]

    now[0] += 1.0
    gate.send(Command("incandescent_lamp", "ON"), Actor.USER)
    second_row = store.connection.execute(
        "SELECT rowid FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()["rowid"]
    assert first_row != second_row

    # No sweep_timeouts() call anywhere above: the first row is already
    # settled by the second send() itself.
    first = store.connection.execute(
        "SELECT outcome, reason FROM actions WHERE rowid = ?", (first_row,)
    ).fetchone()
    assert first["outcome"] == "TIMEOUT"
    assert first["reason"]
    assert "supersed" in first["reason"].lower()

    second = store.connection.execute(
        "SELECT outcome FROM actions WHERE rowid = ?", (second_row,)
    ).fetchone()
    assert second["outcome"] == "SENT"

    # The second row still reaches TIMEOUT the ordinary way once ITS OWN
    # window elapses - the displacement settlement does not short-circuit
    # the normal path for whichever command is actually still tracked.
    now[0] += 30.0
    gate.sweep_timeouts()
    assert store.connection.execute(
        "SELECT outcome FROM actions WHERE rowid = ?", (second_row,)
    ).fetchone()["outcome"] == "TIMEOUT"


def test_confirming_the_newer_command_does_not_touch_the_superseded_row(store):
    """The other half of finding 4: once the displaced first row is
    settled to TIMEOUT by the supersession itself, a reply confirming the
    SECOND command must not reach back and rewrite it - proving confirm()
    resolves against the exact rowid it is tracking, not a device-wide
    lookup that could still match the first, already-settled row."""
    now = [1000.0]
    registry = Registry(store)
    registry.seed_defaults()
    gate = Gate(registry, store, publisher=SpyPublisher(), clock=lambda: now[0])

    gate.send(Command("incandescent_lamp", "OFF"), Actor.RULE, "left_on")
    first_row = store.connection.execute(
        "SELECT rowid FROM actions ORDER BY ts DESC LIMIT 1"
    ).fetchone()["rowid"]

    now[0] += 1.0
    gate.send(Command("incandescent_lamp", "ON"), Actor.USER)
    assert store.connection.execute(
        "SELECT outcome FROM actions WHERE rowid = ?", (first_row,)
    ).fetchone()["outcome"] == "TIMEOUT"  # settled by the supersession itself

    gate.confirm("plug_lamp", "ON")

    outcomes = {
        row["rowid"]: row["outcome"]
        for row in store.connection.execute("SELECT rowid, outcome FROM actions")
    }
    assert outcomes[first_row] == "TIMEOUT"
    assert all(
        outcome == "CONFIRMED"
        for rowid, outcome in outcomes.items()
        if rowid != first_row
    )
