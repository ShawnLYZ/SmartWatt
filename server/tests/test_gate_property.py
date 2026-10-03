"""The guarantee, four ways.

1. A PROPERTY over generated rule sets and generated device registries:
   no combination can produce a cut command for a protected device.
2. A MUTATION test that breaks the gate and asserts the property FAILS.
   A property that never fails proves nothing.
3. A RUNTIME test that reaches the public `app.state.mqtt` handle exactly
   as a bypass module would, and watches the command publish REFUSED.
4. An ARCHITECTURE test asserting no module but the gate's own path
   publishes a command -- matching by full path, and looking for the
   publish CALL and the paho import, not only for one string literal.

3 and 4 answer the same threat from opposite sides, and neither is
redundant: a scan cannot see a topic assembled from segments at runtime,
and a runtime refusal cannot see a module that brings its own client.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from smartwatt_server.api import create_app
from smartwatt_server.config import settings as load_settings
from smartwatt_server.gate import Actor, Command, CommandAuthority, Gate, Outcome
from smartwatt_server.ingest import (
    CommandAuthorityRefused,
    CommandTopicRefused,
    Ingestor,
    MqttIngest,
)
from smartwatt_server.ledger import LedgerIntegrator
from smartwatt_server.registry import Device, Registry
from smartwatt_server.store import Store

PACKAGE = Path(__file__).resolve().parents[1] / "smartwatt_server"

SAFE_ID = st.from_regex(r"\A[a-z][a-z0-9_]{2,15}\Z", fullmatch=True)


class SpyPublisher:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, topic: str, payload: str, authority=None) -> bool:
        self.sent.append((topic, payload))
        return True


devices = st.builds(
    Device,
    appliance_id=SAFE_ID,
    display_name=st.just("generated"),
    plug_device=st.one_of(st.none(), SAFE_ID.map(lambda s: f"plug_{s}")),
    protected=st.booleans(),
    heating=st.booleans(),
)


@st.composite
def _registries(draw) -> list[Device]:
    """A device catalogue that always contains a protected device with a
    plug and a heating device with a plug, alongside a normal random
    spread of other devices around them - and where every plugged
    device's `plug_device` is unique across the WHOLE catalogue, not
    just its `appliance_id`.

    `devices` above makes `protected`, `heating` and "has a plug" three
    independent coin flips, so a plain `st.lists(devices, ...)` catalogue
    frequently contained no protected+plugged device at all - measured
    directly at roughly half of all generated catalogues (see the fix
    report for the numbers). This guarantees the two shapes the
    properties below are actually about while still drawing their id,
    plug name, and however many OTHER devices join them (0-6, each
    independently protected/heating/plugged or not) at random - the
    point is a property over generated registries, not two fixed
    devices standing in for one.

    The plug-uniqueness guarantee is not optional: an earlier version of
    this composite drew the protected and heating devices' plug names
    independently, and Hypothesis promptly found a catalogue where both
    landed on the same `plug_device`. That produces a FALSE positive
    failure with the real, correct gate.py - a command legitimately sent
    to the non-protected device (e.g. heating=True, OFF) publishes to a
    plug the protected device also happens to claim, and the property's
    plug-keyed assertion cannot tell the two apart.

    This composite excluding that shape is not just a generator-side
    opinion: `appliances.plug_device` carries a UNIQUE constraint (see
    store.py's DDL and `test_upsert_appliance_rejects_two_appliances_on_
    the_same_plug` in test_store.py), so a catalogue with two appliances
    on one plug is not a registry `Store` can ever actually hold. The
    generator excluding it matches what the system enforces, rather than
    an invariant assumed independently of it - a task 3 review finding:
    the first version of this comment asserted the invariant on its own
    authority, and nothing backed it until the constraint was added.
    """
    protected_id, heating_id = draw(
        st.lists(SAFE_ID, min_size=2, max_size=2, unique=True)
    )
    protected_plug_id, heating_plug_id = draw(
        st.lists(SAFE_ID, min_size=2, max_size=2, unique=True)
    )
    protected_plug, heating_plug = f"plug_{protected_plug_id}", f"plug_{heating_plug_id}"
    guaranteed = [
        Device(protected_id, "generated", protected_plug, protected=True, heating=False),
        Device(heating_id, "generated", heating_plug, protected=False, heating=True),
    ]

    extra = draw(st.lists(devices, max_size=6, unique_by=lambda d: d.appliance_id))
    reserved_ids = {protected_id, heating_id}
    reserved_plugs = {protected_plug, heating_plug}
    for device in extra:
        if device.appliance_id in reserved_ids:
            continue
        if device.plug_device is not None and device.plug_device in reserved_plugs:
            continue
        guaranteed.append(device)
        reserved_ids.add(device.appliance_id)
        if device.plug_device is not None:
            reserved_plugs.add(device.plug_device)

    return guaranteed


registries = _registries()


@st.composite
def _catalogue_and_targeted_command(draw) -> tuple[list[Device], Command]:
    """Draws a catalogue, then a command whose appliance_id names one of
    THAT catalogue's own devices at high weight (80%) - the branch the
    properties below actually claim to test - with a low-weight tail
    (20%, split between a fresh probably-absent id, `__residual__` and
    `unknown_1`) that keeps the refusal paths covered without letting
    them dominate the run.

    Before this, the command's appliance_id and the catalogue's device
    ids were drawn independently from the same regex space, so a
    generated command named a generated device only by coincidence -
    about 1% of examples, measured directly. The property then spent the
    overwhelming majority of its examples proving an UNREGISTERED id
    gets refused: true, but not the property its name and assertion
    claim to test. See the fix report for the measured before/after hit
    rates and the broken-guard tally that proves this version actually
    catches what it claims to.
    """
    catalogue = draw(registries)
    catalogue_ids = [device.appliance_id for device in catalogue]

    roll = draw(st.integers(min_value=0, max_value=99))
    if roll < 80:
        appliance_id = draw(st.sampled_from(catalogue_ids))
    elif roll < 87:
        appliance_id = draw(SAFE_ID)  # probably absent from the catalogue
    elif roll < 93:
        appliance_id = "__residual__"
    else:
        appliance_id = "unknown_1"

    command = draw(st.builds(
        Command, appliance_id=st.just(appliance_id),
        command=st.sampled_from(["ON", "OFF"]),
    ))
    return catalogue, command


catalogues_and_commands = _catalogue_and_targeted_command()

rule_names = st.one_of(st.none(), st.text(min_size=1, max_size=20))


@contextmanager
def _gate_over(db_dir: Path, catalogue: list[Device], publisher: SpyPublisher):
    """Open a fresh Store for one generated example, yield a Gate built
    over it, and close the Store no matter how the caller exits.

    Hypothesis calls the property tests below several hundred times (300
    + 200 + 200 examples across the three @given tests in this module).
    Opening a Store per example and never closing it would leak one
    SQLite connection - plus its WAL sidecar files - per example. On
    Windows an open handle also blocks pytest's own tmp-directory
    cleanup, so the leak would surface as teardown noise as well as
    exhausted handles over a run this size. A context manager keeps
    "open, seed, use, close" scoped to exactly one example, the same way
    the `store` fixture in conftest.py scopes it to one ordinary test.

    Seeds through `Store.upsert_appliance` rather than a raw INSERT: that
    is the same locked write path `Registry.seed_defaults()` uses in
    production, so the property exercises real machinery instead of a
    parallel shortcut, and it takes its columns by name instead of by
    position.
    """
    store = Store()
    store.open(db_dir / "prop.db")
    try:
        for device in catalogue:
            store.upsert_appliance(
                appliance_id=device.appliance_id,
                display_name=device.display_name,
                protected=device.protected,
                heating=device.heating,
                plug_device=device.plug_device,
            )
        yield Gate(Registry(store), store, publisher=publisher)
    finally:
        store.close()


# -- 1. the property --------------------------------------------------------

# deadline=None on all three @given tests below: each example opens and
# closes a real file-backed SQLite Store (see _gate_over), and that I/O
# occasionally exceeds Hypothesis's default 200ms deadline under ordinary
# filesystem jitter - observed on this machine as a DeadlineExceeded /
# FlakyFailure on an otherwise-passing example (305ms once, 8.7ms on
# replay). The deadline exists to catch accidental algorithmic blowups in
# the code under test, not to characterise per-example disk I/O, so
# disabling it here is the same trade Hypothesis's own docs recommend for
# any property that talks to a real datastore.
@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(catalogue_and_cmd=catalogues_and_commands, actor=st.sampled_from(list(Actor)),
       rule=rule_names)
def test_no_configuration_can_cut_a_protected_load(
    tmp_path_factory, catalogue_and_cmd, actor, rule
):
    """THE property. Over generated rule sets and generated registries, no
    combination produces a SENT outcome with OFF for a protected device."""
    catalogue, cmd = catalogue_and_cmd
    publisher = SpyPublisher()
    protected_plugs = {
        device.plug_device
        for device in catalogue
        if device.protected and device.plug_device
    }

    with _gate_over(tmp_path_factory.mktemp("prop"), catalogue, publisher) as gate:
        gate.send(cmd, actor, rule)

    for topic, payload in publisher.sent:
        plug = topic.split("/")[1]
        assert not (plug in protected_plugs and payload == "OFF"), (
            f"the gate let an OFF command reach protected plug {plug!r}"
        )


@settings(
    max_examples=200,
    deadline=None,  # see the comment on the property test above
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(catalogue_and_cmd=catalogues_and_commands, actor=st.sampled_from(list(Actor)))
def test_no_configuration_can_energise_a_heating_load(
    tmp_path_factory, catalogue_and_cmd, actor
):
    catalogue, cmd = catalogue_and_cmd
    publisher = SpyPublisher()
    heating_plugs = {
        device.plug_device
        for device in catalogue
        if device.heating and device.plug_device
    }

    with _gate_over(tmp_path_factory.mktemp("heat"), catalogue, publisher) as gate:
        gate.send(cmd, actor)

    for topic, payload in publisher.sent:
        plug = topic.split("/")[1]
        assert not (plug in heating_plugs and payload == "ON")


@settings(
    max_examples=200,
    deadline=None,  # see the comment on the property test above
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(catalogue_and_cmd=catalogues_and_commands, actor=st.sampled_from(list(Actor)))
def test_every_call_is_logged_whatever_the_outcome(
    tmp_path_factory, catalogue_and_cmd, actor
):
    catalogue, cmd = catalogue_and_cmd
    publisher = SpyPublisher()
    with _gate_over(tmp_path_factory.mktemp("log"), catalogue, publisher) as gate:
        gate.send(cmd, actor)
        count = gate._store.connection.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM actions"
        ).fetchone()[0]
    assert count == 1


# -- 2. the mutation --------------------------------------------------------

def test_the_property_has_teeth(tmp_path, monkeypatch):
    """Deliberately break the protected check and assert the property FAILS.

    Without this, a property that always passes is indistinguishable from a
    property that cannot fail - and a safety test that cannot fail is worse
    than no safety test at all.
    """
    import smartwatt_server.gate as gate_module

    catalogue = [
        Device("laptop_charger", "Laptop charger", "plug_laptop",
               protected=True, heating=False),
    ]
    publisher = SpyPublisher()

    # Sanity: intact, the gate refuses.
    with _gate_over(tmp_path, catalogue, publisher) as gate:
        assert gate.send(Command("laptop_charger", "OFF"), Actor.USER).outcome is (
            Outcome.REFUSED_PROTECTED
        )
    assert publisher.sent == []

    # Now break it: make every device report itself unprotected.
    original = gate_module.Registry.get

    def broken_get(self, appliance_id):
        device = original(self, appliance_id)
        if device is None:
            return None
        return Device(device.appliance_id, device.display_name,
                      device.plug_device, protected=False,
                      heating=device.heating)

    monkeypatch.setattr(gate_module.Registry, "get", broken_get)

    with _gate_over(tmp_path / "broken", catalogue, publisher) as broken:
        broken.send(Command("laptop_charger", "OFF"), Actor.USER)

    assert publisher.sent, (
        "breaking the protected check MUST let a cut through. If it does "
        "not, the property is not actually testing the check."
    )
    assert ("cmnd/plug_laptop/POWER", "OFF") in publisher.sent


# -- 3. the runtime refusal -------------------------------------------------
#
# The architecture scan below is a source scan, and a source scan cannot
# be the whole guarantee: a bypass that reaches `app.state.mqtt` and
# assembles `cmnd/<plug>/POWER` from segments is invisible to any grep for
# the literal. These two tests are the half that does not depend on
# reading source at all -- the general publish path REFUSES a command
# topic at runtime, on the same object a bypass would reach, through a
# real assembled app.


class _StubPahoClient:
    """Stands in for the paho client `MqttIngest._run()` would construct.

    Assigned onto `_client` directly, the same way
    test_ingest.py::test_publish_delegates_to_the_paho_client does: a real
    one needs a broker, and what matters here is only whether a topic
    reaches it at all.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload))
        return SimpleNamespace(rc=0)  # paho's MQTT_ERR_SUCCESS

    def disconnect(self):
        # lifespan shutdown calls MqttIngest.stop(), which calls this on
        # whatever _client holds. No thread was ever started, so there is
        # nothing else for stop() to wait on.
        pass


@contextmanager
def _app_with_a_real_client(store, monkeypatch):
    """A real assembled app whose `app.state.mqtt` is a REAL MqttIngest,
    authorised by the real lifespan wiring.

    `start_ingest=True` so `create_app`'s own lifespan constructs the
    client and performs the `take_command_authority()` handover -- the
    production path, not an approximation of it built here. `start()` is
    neutered so no thread and no broker are involved, and a stub stands
    in for the paho client `_run()` would have built.

    Hand-wiring an MqttIngest in the test instead would prove nothing
    about how api.py wires one: it was exactly that shortcut, in the
    first version of these tests, that let an UNAUTHORISED client stand
    in for the real thing and hid the Critical.
    """
    monkeypatch.setattr(MqttIngest, "start", lambda self: None)
    app = create_app(store=store, start_ingest=True)
    with TestClient(app) as client:
        stub = _StubPahoClient()
        client.app.state.mqtt._client = stub  # noqa: SLF001 - stands in for _run()
        yield client, stub


def test_reaching_the_public_mqtt_handle_cannot_publish_a_command(
    store, monkeypatch,
):
    """The bypass the final review demonstrated, and the one the FIRST
    fix for it left open, against a real app with real wiring.

    A module that imports nothing from gate.py and reaches the PUBLIC
    `app.state.mqtt` handle must not be able to switch a relay by ANY
    method on that object:

      * `publish()` with a command topic assembled from segments -- so
        nothing about it is visible to a scan for the literal `cmnd/`;
      * `publish_command()`, which the first fix guarded only by topic
        SHAPE. A bypass chooses its own topic, so a topic-shaped check
        authorises it exactly as readily as it authorises the gate.
        Presenting a `CommandAuthority` it constructed itself is the
        closest a bypass can get, and identity is what refuses it.

    Every case asserts BOTH that the call raises AND that nothing reached
    the client, because a refusal after the publish would be no refusal.
    """
    with _app_with_a_real_client(store, monkeypatch) as (client, stub):
        mqtt = client.app.state.mqtt

        bypass_topic = "/".join(("cmnd", "plug_laptop", "POWER"))
        with pytest.raises(CommandTopicRefused):
            mqtt.publish(bypass_topic, "OFF")

        with pytest.raises(CommandAuthorityRefused):
            mqtt.publish_command(bypass_topic, "OFF", CommandAuthority())

        with pytest.raises(CommandAuthorityRefused):
            mqtt.publish_command(bypass_topic, "OFF", None)

        assert stub.published == [], (
            "a refusal must happen BEFORE the client is reached, not after"
        )

        # And the gate's own path, through the same object, still works --
        # otherwise "commands are refused" would be trivially satisfiable
        # by refusing everything.
        response = client.post(
            "/api/control",
            json={"appliance_id": "incandescent_lamp", "command": "OFF"},
        )
        assert response.json()["outcome"] == "SENT"
        assert stub.published == [("cmnd/plug_lamp/POWER", "OFF")]


def test_a_bypass_cannot_cut_a_protected_appliance(store, monkeypatch):
    """The Critical, as it was actually reproduced -- now refused.

    The reproduction was three statements: the gate refuses the protected
    cut, the next line publishes it anyway through the public handle, and
    `actions` holds nothing about it. This asserts all three of the
    things that made that a Critical rather than a smell -- the relay
    does not switch, the gate's own refusal IS recorded, and the bypass
    leaves the log exactly as the gate left it, so no evidence is
    produced OR destroyed by an attempt the system did not honour.
    """
    with _app_with_a_real_client(store, monkeypatch) as (client, stub):
        refused = client.post(
            "/api/control",
            json={"appliance_id": "laptop_charger", "command": "OFF"},
        ).json()
        assert refused["outcome"] == "REFUSED_PROTECTED"
        assert stub.published == []

        before = client.get("/api/actions").json()
        assert [row["outcome"] for row in before] == ["REFUSED_PROTECTED"]

        with pytest.raises(CommandAuthorityRefused):
            client.app.state.mqtt.publish_command(
                "cmnd/plug_laptop/POWER", "OFF", CommandAuthority()
            )

        assert stub.published == [], (
            "a protected appliance was cut by a caller that never passed "
            "through the gate"
        )
        assert client.get("/api/actions").json() == before


def test_the_gates_capability_is_handed_out_exactly_once(store, monkeypatch):
    """`app.state.gate` is public, so the handover must not be repeatable.

    Without the one-shot, `take_command_authority()` would BE the public
    accessor the capability exists in order not to have: a bypass would
    ask the gate for the authority and hand it straight to the client.
    """
    with _app_with_a_real_client(store, monkeypatch) as (client, _stub):
        with pytest.raises(RuntimeError):
            client.app.state.gate.take_command_authority()


def test_one_gates_authority_is_never_another_gates(store):
    """Identity, not type. `CommandAuthority` is an ordinary class any
    module can instantiate -- what cannot be obtained is THE instance a
    given client was authorised with, so no two gates share one.
    """
    registry = Registry(store)
    one = Gate(registry, store, publisher=SpyPublisher())
    two = Gate(registry, store, publisher=SpyPublisher())
    assert one.take_command_authority() is not two.take_command_authority()


def test_the_command_entry_point_will_not_carry_anything_else(store):
    """`publish_command` is not a quieter general-purpose publisher.

    Without this, a caller that noticed `publish()` had grown a guard
    could simply move every publish onto `publish_command()` and the
    distinction between the two paths would collapse. Presented WITH a
    valid authority, so what is under test is the topic check itself and
    not the authority check standing in front of it.
    """
    gate = Gate(Registry(store), store)
    authority = gate.take_command_authority()
    mqtt_ingest = MqttIngest(
        load_settings(),
        Ingestor(store, LedgerIntegrator(store)),
        command_authority=authority,
    )
    mqtt_ingest._client = _StubPahoClient()  # noqa: SLF001 - stands in for _run()

    with pytest.raises(ValueError):
        mqtt_ingest.publish_command("smartwatt/telemetry", "{}", authority)
    assert mqtt_ingest._client.published == []  # noqa: SLF001

    # The ordinary path still carries an ordinary topic.
    assert mqtt_ingest.publish("smartwatt/telemetry", "{}") is True


def test_a_command_is_refused_even_with_no_client_at_all(store):
    """The refusal is unconditional, not a side effect of the broker.

    `publish()` reports False (never raises) when no client exists, so a
    guard placed AFTER that check would let the bypass above return a
    quiet False in every start_ingest=False deployment and in every test
    -- looking exactly like a disconnected broker.
    """
    mqtt_ingest = MqttIngest(load_settings(), Ingestor(store, LedgerIntegrator(store)))
    assert mqtt_ingest._client is None  # noqa: SLF001
    with pytest.raises(CommandTopicRefused):
        mqtt_ingest.publish("cmnd/plug_lamp/POWER", "OFF")


# -- 4. the architecture ----------------------------------------------------

#: Which files in the package may do which of the three things below.
#: Keyed by path RELATIVE TO THE PACKAGE, never by basename: the review
#: exempted a bypass simply by naming it `gate.py` inside a subpackage,
#: because the old scan compared `path.name`.
_MAY_NAME_A_COMMAND_TOPIC = frozenset({"gate.py", "ingest.py"})
_MAY_IMPORT_PAHO = frozenset({"ingest.py"})
_MAY_CALL_PUBLISH = frozenset({"ingest.py", "api.py"})

#: MQTT publish methods. `Bus.publish_threadsafe` is deliberately NOT in
#: here and deliberately not matched by prefix -- it is the WebSocket
#: fan-out, reaches no broker, and folding it in would force api.py's bus
#: call to be exempted too. `publish_retained` IS in here: it is a public
#: broker method (the fingerprint push's retained publish), so a reference
#: to it anywhere but ingest.py and api.py is a new path to the wire.
_PUBLISH_METHODS = frozenset({"publish", "publish_command", "publish_retained"})


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the Constant nodes that are docstrings, so prose about the
    mechanism is never mistaken for the mechanism.

    The old scan skipped lines that START with `#` or `\"\"\"`, which let
    the second and later lines of any docstring through and forced the
    prose in api.py and gate.py to talk around the literal it was
    describing. Parsing instead means comments do not reach the tree at
    all and docstrings are identified exactly.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _package_modules() -> list[tuple[str, ast.AST]]:
    modules = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        modules.append((relative, ast.parse(path.read_text(encoding="utf-8"))))
    assert len(modules) >= 12, f"the scan found only {len(modules)} modules"
    return modules


def test_only_the_gate_publishes_commands():
    """No module but `gate.py` may publish a command, and this scan looks
    for the CALL, not for one string literal.

    The spec's Risks section makes this test the thing that carries the
    structural guarantee forward as the codebase grows, so it is strict on
    purpose and runs in the same suite as everything else, never as an
    optional lint. Three separate offences, because the final review got
    past the single-string version three different ways:

      * a command-topic literal outside gate.py/ingest.py -- the original
        check, kept;
      * an `import paho` outside ingest.py, which is how a new module
        would get its own client and bypass MqttIngest entirely;
      * a `.publish`/`.publish_command` REFERENCE outside ingest.py and
        api.py, which is what the review's `app.state.mqtt` bypass
        actually did, whatever it named its topic variable.

    Every exemption is by full relative path, so `subpkg/gate.py` is not
    `gate.py`.

    Scope is this package. The simulator is a separate PROCESS that
    publishes telemetry to the same broker and never publishes a command;
    the guarantee this test carries is about what the server can emit.

    WHAT THIS CANNOT SEE, stated rather than implied: a name resolved at
    runtime -- `getattr(app.state.mqtt, "publish_command")` -- has no
    matching node in any tree, and no source scan in Python can find one.

    An earlier version of this docstring said that blind spot was covered
    "by the runtime refusal". That was true of `publish()`, which refuses
    on the topic, and FALSE of `publish_command()`, which at the time
    checked the topic and not the caller -- so resolving the name
    dynamically reached a method that would publish. It no longer does:
    `publish_command` refuses on the CALLER's capability, and how the
    caller spelled the method name has no bearing on whether it holds the
    gate's `CommandAuthority`. Dynamic resolution therefore buys a bypass
    nothing that the direct call does not already fail at.

    What remains genuinely uncovered, by this test and by the runtime
    checks alike, is a caller that reaches through a private attribute --
    `_MqttIngest__command_authority` -- and presents what it finds. Python
    has no unforgeable references; the spec's Risks section says so, and
    the pair of mechanisms, not this test alone, is what carries the
    guarantee.
    """
    offenders: list[str] = []

    for relative, tree in _package_modules():
        docstrings = _docstring_nodes(tree)

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and (node.value == "cmnd" or "cmnd/" in node.value)
                and relative not in _MAY_NAME_A_COMMAND_TOPIC
            ):
                offenders.append(
                    f"{relative}:{node.lineno}: command topic literal "
                    f"{node.value!r}"
                )

            if relative not in _MAY_IMPORT_PAHO:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "paho":
                            offenders.append(
                                f"{relative}:{node.lineno}: import {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom) and (node.module or "").split(
                    "."
                )[:1] == ["paho"]:
                    offenders.append(
                        f"{relative}:{node.lineno}: from {node.module} import ..."
                    )

            # Any REFERENCE, not only a call: `send = mqtt.publish_command`
            # followed by `send(topic, payload)` is a call node whose func
            # is a plain Name, and matching calls alone would miss it.
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _PUBLISH_METHODS
                and relative not in _MAY_CALL_PUBLISH
            ):
                offenders.append(
                    f"{relative}:{node.lineno}: .{node.attr} reference"
                )

    assert offenders == [], (
        "only the safety gate's own path may publish a command; found:\n"
        + "\n".join(offenders)
    )


def test_the_composition_root_has_exactly_one_command_call_site():
    """api.py is exempted above because it must wire the gate to the
    client -- and this is what keeps that exemption from being a hole.

    api.py may name `publish_command` exactly once (the `_publish_command`
    closure handed to Gate) -- the ONE command call site -- and
    `publish_retained` exactly once (the `_publish_retained` closure behind
    the fingerprint push, which goes through `publish`'s command-topic
    refusal and never carries a command). It may not name the general
    `publish` at all. A new route that publishes -- the easiest bypass to
    write, in the one file allowed to hold the client handle -- fails here.
    """
    tree = ast.parse((PACKAGE / "api.py").read_text(encoding="utf-8"))
    references = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in _PUBLISH_METHODS
    ]
    assert sorted(references) == ["publish_command", "publish_retained"], (
        "api.py must name exactly two publish methods, each once: "
        f"publish_command (the one command call site) and publish_retained; "
        f"found {references}"
    )
    assert references.count("publish_command") == 1


def test_the_publisher_is_private_to_the_gate(store):
    """No PUBLIC attribute of a live Gate hands out the publisher.

    The previous version asserted `"self.publisher" not in` gate.py's
    source, which the final review walked straight past: a
    `@property publisher` returning `self._publisher` contains no such
    substring, and the test stayed green while the client was public.

    This one asks the object instead of the file. `dir()` covers
    properties, plain attributes and methods alike, and every public one
    is evaluated -- so anything that returns the publisher, by whatever
    route, is caught.
    """
    registry = Registry(store)
    registry.seed_defaults()
    publisher = SpyPublisher()
    gate = Gate(registry, store, publisher=publisher)

    assert gate._publisher is publisher  # noqa: SLF001 - the private one still works

    # The CAPABILITY is checked here too, not only the publisher. It is
    # the other thing that authorises a command, so a public attribute
    # handing it out would be the same hole wearing a different name --
    # and `take_command_authority()` is deliberately a one-shot METHOD
    # rather than a property for exactly this reason (getattr below never
    # calls it; test_the_gates_capability_is_handed_out_exactly_once
    # covers that route).
    secrets = {id(publisher): "publisher", id(gate._authority): "authority"}  # noqa: SLF001

    exposed = []
    for name in dir(gate):
        if name.startswith("_"):
            continue
        try:
            value = getattr(gate, name)
        except Exception:  # noqa: BLE001 - a raising property exposes nothing
            continue
        if id(value) in secrets:
            exposed.append(f"{name} -> {secrets[id(value)]}")
    assert exposed == [], (
        "neither the publisher nor the command authority may be reachable "
        f"from a public attribute; exposed via {exposed}"
    )
