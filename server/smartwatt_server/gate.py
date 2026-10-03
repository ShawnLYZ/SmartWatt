"""THE SAFETY GATE.

`send()` is the only function in this system that turns an intent to
switch a plug into a command on the wire. The rules engine calls it. The
manual API calls it. Nothing else does, and nothing may.

WHAT ACTUALLY ENFORCES THAT, mechanism by mechanism. This module does NOT
own the MQTT client - it never did. The client lives in `ingest.py`, on
an `MqttIngest` that `api.py` publishes at `app.state.mqtt`, where every
module in the process can reach it. What this module holds is a closure
over that object, privately, as `self._publisher`. So the guarantee rests
on three things instead of on ownership:

  1. RUNTIME, AND ON THE CALLER. `MqttIngest.publish()`, the
     general-purpose path any other module would reach for, RAISES on a
     `cmnd/` topic instead of publishing it - before it checks for a
     client, before it looks at the broker. The only method that will put
     a command topic on the wire is `MqttIngest.publish_command()`, and
     it refuses any caller that does not present THE `CommandAuthority`
     object that client was authorised with - the one this Gate
     constructs, keeps privately, and hands to `api.py`'s wiring exactly
     once.

     Both halves are needed, and the second was learned the hard way: a
     first version of this guarded `publish_command` by TOPIC SHAPE
     alone, and a three-line reproduction cut a protected appliance
     through the public `app.state.mqtt` with no gate check and no
     `actions` row. A bypass picks its own topic and its own file name,
     so every check on the CALL is a check it controls. Which object it
     holds is not.

     A module that reaches `app.state.mqtt` and builds `cmnd/<plug>/POWER`
     - however it builds it, including by joining segments no grep would
     recognise - now fails where it stands, on either method.
  2. TOPIC CONSTRUCTION. This module is where a command topic is built,
     and (with `ingest.py`, which names the prefix in order to refuse it)
     one of only two files permitted to contain the literal at all.
  3. THE FOUR CHECKS BELOW, in `send()`, which no caller can skip because
     no caller has the publisher to skip them with.

Together that is the structural property non-negotiable #3 requires: no
rule configuration, and no mistake in one, can produce a cut command for
a protected device - not because anyone was careful, but because the
paths that would have to do it either do not exist or refuse at runtime.

Three tests defend the property as the codebase grows:
  * a runtime test, reaching the public `app.state.mqtt` of a real app
    exactly as a bypass would, and asserting the publish is refused
  * an architecture test, scanning by full path for publish calls, paho
    imports and command-topic literals outside the permitted modules
  * a mutation test, which breaks the protected check and asserts the
    Hypothesis property FAILS - a property that never fails proves nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NamedTuple

from .registry import Registry
from .store import Store

log = logging.getLogger(__name__)

#: A command with no `stat/` reply inside this window is logged TIMEOUT,
#: never assumed successful.
CONFIRM_TIMEOUT_S = 15.0


def _normalise_power(value: object) -> Literal["ON", "OFF"] | None:
    """Canonicalise a raw power value to exactly ``"ON"`` or ``"OFF"``, or
    ``None`` if it is neither.

    Tasmota's own POWER vocabulary is case-insensitive, so a differently
    cased or whitespace-padded ON/OFF (``"off"``, ``" On "``) is accepted
    and canonicalised to upper case. Everything else - ``"TOGGLE"``,
    ``"0"``/``"1"``/``"2"``, an empty string, a type that is neither
    ``str`` nor ``bytes``, or ``bytes`` that fail to decode - returns
    ``None`` rather than raising.

    Deliberately shared by two callers with opposite reactions to that
    ``None``: ``Command.__post_init__`` treats it as a construction error
    (a value that cannot even resolve to ON or OFF must never become a
    live command), and ``Gate.confirm`` treats it as "not a confirmation"
    (a plug's own reply must never be allowed to crash the thread that
    delivered it - see `ingest.py`'s note on what an exception escaping
    paho's callback costs).
    """
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    canonical = value.strip().upper()
    if canonical == "ON":
        return "ON"
    if canonical == "OFF":
        return "OFF"
    return None


class CommandAuthority:
    """The capability that authorises a command publish.

    POSSESSION OF THIS OBJECT is what `MqttIngest.publish_command` checks,
    by identity, before it will put a `cmnd/` topic on the wire. Not the
    shape of the topic, and not the name of the file the call is written
    in - both of those are properties of the CALL, and the final review
    demonstrated that a bypass controls both of them. Who is calling is
    the only thing a bypass cannot forge, so that is what is checked.

    Each `Gate` constructs exactly one, keeps it privately, and presents
    it on every publish. `Gate.take_command_authority()` hands it to the
    single wiring point that registers it with the client, once. There is
    no other way to obtain one that any client will accept: a bypass can
    construct `CommandAuthority()` freely, and the object it gets is not
    the object the client was given, so the check fails on identity.

    Empty on purpose. It carries no state, no method and no data, because
    it is not a thing that DOES anything - it is a thing you either have
    or do not.
    """

    __slots__ = ()


class Actor(StrEnum):
    RULE = "rule"
    USER = "user"


class Outcome(StrEnum):
    SENT = "SENT"
    CONFIRMED = "CONFIRMED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    REFUSED_PROTECTED = "REFUSED_PROTECTED"
    REFUSED_HEATING_ONE_WAY = "REFUSED_HEATING_ONE_WAY"
    REFUSED_UNREGISTERED = "REFUSED_UNREGISTERED"
    REFUSED_NOT_CONTROLLABLE = "REFUSED_NOT_CONTROLLABLE"


@dataclass(frozen=True, slots=True)
class Command:
    appliance_id: str
    command: Literal["ON", "OFF"]

    def __post_init__(self) -> None:
        # `Literal["ON", "OFF"]` is a type-checker-only promise: it is
        # erased at runtime, so nothing before this stopped
        # `Command(id, "off")` or `Command(id, "toggle")` from being
        # constructed. Normalising and validating here means "cmd.command
        # is exactly ON or OFF" holds for every Command that exists, not
        # just ones a type checker happened to see - which matters because
        # the guards in Gate.send() are written to trust it.
        canonical = _normalise_power(self.command)
        if canonical is None:
            raise ValueError(
                f"Command.command must resolve to ON or OFF, got {self.command!r}"
            )
        object.__setattr__(self, "command", canonical)


@dataclass(frozen=True, slots=True)
class Result:
    outcome: Outcome
    reason: str | None
    plug_device: str | None


class _Awaiting(NamedTuple):
    """One outstanding command: what was sent, which row logged it, and
    when.

    `confirm()` needs `command` to tell a genuine confirmation from a
    reply that carries the opposite of what was asked. `rowid` is the
    exact `actions` row `send()` wrote for this command, so a confirmation
    or a sweep settles THAT row - not "whatever the most recent row for
    this plug happens to be", which resolves to the wrong row once a
    second command to the same plug is sent before the first settles.
    `sent_at` is what `sweep_timeouts()` compares against the clock.
    """

    command: Literal["ON", "OFF"]
    rowid: int
    sent_at: float


class Gate:
    def __init__(
        self,
        registry: Registry,
        store: Store,
        publisher: Callable[[str, str, "CommandAuthority"], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._store = store
        # PRIVATE: no PUBLIC attribute of this object exposes it, which
        # is what test_the_publisher_is_private_to_the_gate checks -- on
        # a live Gate, not by grepping this file, because a `publisher`
        # property returning `self._publisher` would satisfy a grep.
        # It is NOT unreferenced elsewhere: api.py's lifespan builds the
        # closure and its frame outlives this call. What matters is that
        # api.py never puts it on app.state, so no module can obtain it
        # by asking the app for it.
        self._publisher = publisher or _null_publisher
        # The capability this gate presents on every publish. Constructed
        # here so it exists before any wiring can hand it anywhere, and
        # kept private: no public attribute of this object is it (the
        # privacy test checks that on a live Gate), and
        # take_command_authority() below gives it out exactly once.
        self._authority = CommandAuthority()
        self._authority_taken = False
        self._clock = clock
        self._awaiting: dict[str, _Awaiting] = {}
        # Guards every read-modify-write of _awaiting. confirm() (a stat/
        # reply, arriving on whichever thread the MQTT client calls back
        # on) and sweep_timeouts() (the rules loop) both pop entries from
        # it from different threads; unguarded, whichever `del` ran second
        # on an entry the other had already removed raised KeyError - and
        # paho 2.x re-raises whatever escapes on_message, which would
        # interrupt all ingest for a reconnect-with-backoff cycle, not
        # kill the thread outright (see ingest.py's own note on that cost).
        self._awaiting_lock = threading.Lock()

    def send(
        self, cmd: Command, actor: Actor, rule: str | None = None
    ) -> Result:
        """The ONLY way a plug command leaves this process."""
        # 1. Pseudo-appliances are structurally uncontrollable.
        if self._registry.is_pseudo(cmd.appliance_id):
            return self._refuse(
                cmd, actor, rule, Outcome.REFUSED_NOT_CONTROLLABLE,
                f"{cmd.appliance_id} is a ledger entry, not a controllable device",
            )

        device = self._registry.get(cmd.appliance_id)

        # 2. Registered, with a plug.
        if device is None or device.plug_device is None:
            return self._refuse(
                cmd, actor, rule, Outcome.REFUSED_UNREGISTERED,
                f"{cmd.appliance_id} has no registered plug",
            )

        # 3. PROTECTED. This check does not consult `actor`. It refuses an
        #    automated rule and a human clicking a button identically.
        #    Written as "not proven to be an energise" rather than "is a
        #    cut": Command's constructor already guarantees cmd.command is
        #    exactly "ON" or "OFF", but this guard does not lean on that
        #    holding. That redundancy is deliberate belt-and-braces - do
        #    NOT simplify this to `== "OFF"` on the grounds that the
        #    constructor makes them equivalent today. A command this
        #    check cannot prove is an energise is treated as a possible
        #    cut and refused.
        if device.protected and cmd.command != "ON":
            return self._refuse(
                cmd, actor, rule, Outcome.REFUSED_PROTECTED,
                f"{cmd.appliance_id} is protected and can never be cut",
            )

        # 4. HEATING IS ONE-WAY. The system may de-energise a kettle; only
        #    a person re-energises one. Same reasoning as #3: written as
        #    "not proven to be a de-energise", independent of whatever
        #    Command's constructor already guarantees. Deliberate
        #    belt-and-braces, not a simplifiable duplicate of check #3.
        if device.heating and cmd.command != "OFF":
            return self._refuse(
                cmd, actor, rule, Outcome.REFUSED_HEATING_ONE_WAY,
                f"{cmd.appliance_id} is a heating load and is never energised "
                "by this system",
            )

        # The row is written BEFORE the publish attempt, and _awaiting is
        # populated only AFTER a publish that actually reports success.
        # Two failures that ordering used to allow: a raising publisher
        # escaped before any row was written, so the one module whose
        # purpose is leaving evidence left none at all; and a stat/ reply
        # landing between an old pre-publish _awaiting registration and
        # the row's insert would have tried to update a row that did not
        # exist yet.
        rowid = self._log(cmd, actor, rule, Outcome.SENT, None)

        failure = self._publish(f"cmnd/{device.plug_device}/POWER", cmd.command)
        if failure is not None:
            log.error(
                "gate: %s %s never left the process: %s",
                cmd.command, cmd.appliance_id, failure,
            )
            # TIMEOUT, not a new outcome: it is honest about exactly what
            # is known ("unconfirmed"), and the spec's outcome vocabulary
            # is closed at eight members. The failure itself is not lost
            # - it goes into `reason`, the column that exists for it.
            self._store.set_action_outcome(
                rowid, Outcome.SENT.value, Outcome.TIMEOUT.value, reason=failure,
            )
            return Result(Outcome.TIMEOUT, failure, device.plug_device)

        with self._awaiting_lock:
            displaced = self._awaiting.get(device.plug_device)
            self._awaiting[device.plug_device] = _Awaiting(
                cmd.command, rowid, self._clock()
            )

        if displaced is not None:
            # _awaiting holds exactly one outstanding command per plug, so
            # a second send() to the same plug before the first confirmed
            # or timed out overwrites its entry above - and without this,
            # the displaced row would never be seen by sweep_timeouts()
            # again (it only ever iterates what is CURRENTLY in
            # _awaiting) and would sit at SENT forever. Settle it
            # explicitly instead, still holding its identity (captured
            # before the overwrite, above) rather than trying to
            # rediscover it later. TIMEOUT, not a new outcome: no
            # confirmation ever arrived for it. CANCELLED is reserved for
            # the grace-period cancellation flow, not this. The Store
            # call is deliberately outside _awaiting_lock, matching
            # sweep_timeouts() below - Store takes its own lock, and nothing
            # here needs both held at once.
            self._settle_as_timeout(
                displaced,
                reason=(
                    f"superseded by a new {cmd.command} command to "
                    f"{cmd.appliance_id} before this {displaced.command} "
                    "command was confirmed or timed out"
                ),
            )

        return Result(Outcome.SENT, None, device.plug_device)

    def take_command_authority(self) -> CommandAuthority:
        """Hand this gate's capability to the ONE wiring point that
        registers it with the MQTT client. Callable exactly once.

        `api.py`'s lifespan calls this and passes the result straight into
        `MqttIngest`, which keeps it privately and compares every
        `publish_command` caller's authority against it by identity.

        ONE-SHOT, and that is the whole of its value. `app.state.gate` is
        public, so a module that reaches the app can call this too - and
        gets a `RuntimeError` instead of the capability, because the
        wiring already took it. Without the one-shot this method would
        simply be the public accessor the capability exists to not have.

        It does NOT make the capability unreachable in a language sense:
        `gate._authority` is a private attribute, not a locked box, and
        Python has no locked boxes. What it does is remove every route to
        it that does not announce itself - a bypass now has to reach
        through a leading underscore, which the architecture test's scan
        of `.publish_command` references catches at the call it must
        eventually make.
        """
        if self._authority_taken:
            raise RuntimeError(
                "this gate's command authority has already been taken; a "
                "second holder is exactly what it exists to prevent"
            )
        self._authority_taken = True
        return self._authority

    def confirm(self, plug_device: str, state: str) -> None:
        """Handles a `stat/<device>/POWER` reply.

        `state` arrives as whatever paho hands `on_message` - bytes, not
        str, once this is wired up - so it is run through the same
        normaliser `Command` validates against before comparing. Only a
        reply that resolves to the command actually in flight upgrades
        the row to CONFIRMED. Anything else - the OPPOSITE of what was
        commanded, or a value that does not resolve to ON/OFF at all - is
        not a confirmation. It is left SENT rather than upgraded, so
        `sweep_timeouts()` can still catch it and log TIMEOUT rather than
        this method quietly recording a success that never happened, or
        crashing the thread that called it over a malformed payload.
        """
        canonical_state = _normalise_power(state)
        with self._awaiting_lock:
            awaiting = self._awaiting.get(plug_device)
            if awaiting is None:
                log.debug(
                    "confirm for %s with no outstanding command; ignoring",
                    plug_device,
                )
                return
            if canonical_state is None:
                log.warning(
                    "plug %s sent an unrecognised POWER state %r; not confirming",
                    plug_device, state,
                )
                return
            if awaiting.command != canonical_state:
                log.warning(
                    "plug %s replied %s but %s was commanded; not confirming",
                    plug_device, canonical_state, awaiting.command,
                )
                return
            # Unconditional pop: whatever branch above already decided
            # this entry is settled, and a bare `del` here would raise
            # KeyError if sweep_timeouts() had just removed the same key
            # on the other thread.
            self._awaiting.pop(plug_device, None)

        self._store.set_action_outcome(
            awaiting.rowid, Outcome.SENT.value, Outcome.CONFIRMED.value
        )

    def sweep_timeouts(self) -> None:
        """A command with no confirmation is logged TIMEOUT, not assumed
        successful."""
        now = self._clock()
        expired: list[_Awaiting] = []
        with self._awaiting_lock:
            for plug_device, awaiting in list(self._awaiting.items()):
                if now - awaiting.sent_at < CONFIRM_TIMEOUT_S:
                    continue
                self._awaiting.pop(plug_device, None)
                expired.append(awaiting)

        # Store writes happen outside the lock: they take Store's own
        # lock, and there is no reason to hold two locks at once for
        # what is, from here, just a list of already-settled entries.
        for awaiting in expired:
            self._settle_as_timeout(awaiting)

    # -- internals ---------------------------------------------------------

    def _settle_as_timeout(self, awaiting: _Awaiting, reason: str | None = None) -> None:
        """Move one outstanding command's row from SENT to TIMEOUT.

        Shared by sweep_timeouts() (silence past CONFIRM_TIMEOUT_S -- no
        reason given, since the row's own age against the log timestamp
        is the explanation) and send()'s handling of a displaced command
        (a second send to the same plug before the first settled, which,
        unlike a genuine timeout, has an immediate and nameable cause
        worth recording rather than leaving the row to explain itself).
        """
        self._store.set_action_outcome(
            awaiting.rowid, Outcome.SENT.value, Outcome.TIMEOUT.value, reason=reason
        )

    def _publish(self, topic: str, payload: str) -> str | None:
        """Call the private publisher without letting it escape uncaught.

        A publisher that raises (broker down, client not yet connected)
        must not skip the logging that follows it - this module's whole
        purpose is leaving evidence, including of its own failures.
        Returns None on a reported success, or a reason string otherwise.
        """
        try:
            published = self._publisher(topic, payload, self._authority)
        except Exception as exc:
            # Deliberately broad: whatever the publisher throws, send()
            # must still finish logging and return a Result instead of
            # propagating it and skipping the accounting that follows.
            return f"publisher raised {exc!r}"
        if not published:
            return "publisher reported failure"
        return None

    def _refuse(
        self, cmd: Command, actor: Actor, rule: str | None,
        outcome: Outcome, reason: str,
    ) -> Result:
        log.warning("gate refused %s %s: %s", cmd.command, cmd.appliance_id, reason)
        self._log(cmd, actor, rule, outcome, reason)
        return Result(outcome, reason, None)

    def _log(
        self, cmd: Command, actor: Actor, rule: str | None,
        outcome: Outcome, reason: str | None,
    ) -> int:
        # ONE NAMESPACE, always the appliance id. This used to log
        # `plug_device or cmd.appliance_id`, and _refuse could only ever
        # pass None -- so a SENT row said `plug_lamp` while a refusal for
        # the same appliance said `incandescent_lamp`, in one unlabelled
        # column an evaluator reads straight off the Control screen. The
        # two names were unreconcilable from the log alone: nothing in the
        # running system maps one to the other.
        #
        # The appliance id is the right survivor of the two. It is what
        # every caller of send() supplies, what the rules engine and
        # /api/control speak, what a refusal has when there is no plug at
        # all (REFUSED_UNREGISTERED, REFUSED_NOT_CONTROLLABLE), and what
        # /api/appliances names. The plug is not lost: it is one column of
        # the appliances row this id keys, and it is in the published
        # topic. Carrying both here would have meant a second `actions`
        # column, which store.py's _DDL cannot add to a database that
        # already exists -- CREATE TABLE IF NOT EXISTS is a no-op there --
        # so it would have needed either a SCHEMA_VERSION bump, which
        # costs every stored row, or the first ALTER-TABLE migration step
        # in a store that deliberately has none. Steep, for a value that
        # is already recoverable from the row this column names.
        return self._store.write_action(
            self._clock(), actor.value, cmd.appliance_id,
            cmd.command, rule, outcome.value, reason,
        )


def _null_publisher(
    topic: str, payload: str, authority: CommandAuthority
) -> bool:
    log.error("no publisher configured; dropped %s %s", topic, payload)
    return False
