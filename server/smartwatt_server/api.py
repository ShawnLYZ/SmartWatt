"""FastAPI application: routes, WebSocket, static assets, startup self-test.

One origin. The dashboard build is served from server/static/, so there is
no CORS configuration and no development server at demonstration time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
from smartwatt_analysis.fingerprints import MalformedFingerprints, to_matrix
from smartwatt_tariff.selftest import run_selftest

from .appliances import load_appliances
from .bus import Bus
from .config import Settings, settings as load_settings
from .gate import Actor, Command, Gate
from .ingest import Ingestor, MqttIngest
from .ledger import LedgerIntegrator
from .localtime import day_bounds, month_bounds, week_bounds
from .month import month_payload, whatif_payload
from .registry import Registry
from .rules import DEMO_DEFAULTS, RulesEngine, SHIPPED_DEFAULTS
from .schemas import validators
from .store import Store
from .wizard import NoEventToVerify, NoTelemetry, Wizard, WrongStep

log = logging.getLogger(__name__)

_FALLBACK_INDEX = (
    "<!doctype html><title>SmartWatt</title>"
    "<p>Dashboard not built. Run <code>npm run build</code> in dashboard/.</p>"
)

#: How often the rules loop below re-evaluates warn/act conditions and
#: sweeps for unconfirmed commands. Independent of CONFIRM_TIMEOUT_S and
#: every RuleConfig.grace_seconds -- this is just the loop's own polling
#: granularity, not a safety-relevant duration itself.
_RULES_TICK_INTERVAL_S = 1.0


class WhatIfRequest(BaseModel):
    appliance_id: str
    hours: float


class ControlRequest(BaseModel):
    appliance_id: str
    command: Literal["ON", "OFF"]


class CaptureConditions(BaseModel):
    """What only the operator knows. background_w, vrms_mean and freq_mean
    are NOT here: the server measures them from stored telemetry (R24), and
    a client sending them is ignored."""

    session_id: str = ""
    concurrent_ids: str = ""
    notes: str = ""


class CaptureRequest(BaseModel):
    """No `event` (R27b): the server always binds the device's own newest
    stored event on this edge that no earlier capture used."""

    label: str
    edge: Literal["on", "off"]
    conditions: CaptureConditions = Field(default_factory=CaptureConditions)


class ConfirmRequest(BaseModel):
    training_id: str


class VerifyRequest(BaseModel):
    """No `actual` (R27b): the answer is always the device's own, for the
    newest event since the last verification (attributed_to, falling back
    to label)."""

    expected: str


def create_app(
    *,
    store: Store | None = None,
    settings: Settings | None = None,
    start_ingest: bool = True,
) -> FastAPI:
    config = settings or load_settings()
    owned_store = store is None
    the_store = store or Store()
    bus = Bus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 1. Configuration self-test. Empty provenance stops boot.
        run_selftest()
        # 2. Schemas load and compile.
        validators()
        # 2b. The household's appliance list. Read before storage opens, so
        # a file that cannot be used stops boot with nothing to clean up.
        # load_appliances raises ApplianceFileError listing every problem.
        catalogue = load_appliances(config.appliances_path)
        # 3. Storage.
        if owned_store:
            the_store.open(config.db_path)
        # 4. Bus.
        bus.attach_loop(asyncio.get_running_loop())

        # 5. Registry, gate and rules engine. Installed here -- this is the
        # "seed the registry at startup" line Task 1's brief deferred to
        # this task: every route below and the rules loop need the
        # household's devices to exist before the first request or tick().
        # sync(), not seed_defaults(): the registry holds exactly the file's
        # list, so an appliance deleted from it cannot still be switched.
        registry = Registry(the_store)
        registry.sync(catalogue.devices)

        def _publish_command(
            topic: str, payload: str, authority: object
        ) -> bool:
            """The only function in this module that reaches the MQTT
            client, and the only caller anywhere of
            ``MqttIngest.publish_command`` -- the one method that will
            put a `cmnd/` topic on the wire. gate.py builds the whole
            outbound topic string itself (see its own module docstring)
            and hands it here already complete; this only forwards it.
            Passed into Gate's constructor below and referenced nowhere
            else -- never stored on app.state -- so Gate stays the only
            path able to turn a Command into a live message leaving this
            process.

            ``authority`` is the gate's own capability, presented on
            every call and forwarded UNCHANGED -- this closure neither
            stores it nor substitutes one of its own. Both of
            MqttIngest's refusals then stand between a bypass and a
            relay: the general ``publish`` refuses a command topic
            outright, and ``publish_command`` refuses a caller whose
            authority is not the object it was given. A module that
            reaches the public ``app.state.mqtt`` therefore has no method
            on it that will switch a relay (test_gate_property.py proves
            both against a real MqttIngest, through this very app).

            `app.state.mqtt` is None whenever start_ingest=False (every
            existing API test, and any deployment reading from storage
            alone), and MqttIngest reports False rather than raising when
            paho's client is absent or disconnected (see that method's
            own docstring). Either way this reports failure rather than
            raising: Gate.send() already turns a False return into an
            honest, loggable TIMEOUT rather than a false SENT.
            """
            mqtt_ingest = app.state.mqtt
            if mqtt_ingest is None:
                return False
            return mqtt_ingest.publish_command(topic, payload, authority)

        gate = Gate(registry, the_store, publisher=_publish_command)
        rules_config = DEMO_DEFAULTS if config.rules_demo else SHIPPED_DEFAULTS
        rules_engine = RulesEngine(
            gate, registry, the_store, config=rules_config,
            left_on_targets=catalogue.left_on_targets,
            standby_targets=catalogue.standby_targets,
        )

        def _on_payload(kind: str, payload: dict) -> None:
            """Fan out to the WebSocket bus, then feed the rules engine.

            Runs on paho's ingest thread -- Ingestor.handle() calls
            on_payload synchronously from on_message, for every accepted
            telemetry and event payload. RulesEngine.observe() only
            wants telemetry, and locks its own state for exactly this
            cross-thread reason (tick() below runs on the asyncio loop
            instead -- see rules.py's _state_lock docstring), so nothing
            further is needed here.
            """
            bus.publish_threadsafe(kind, payload)
            if kind == "telemetry":
                rules_engine.observe(payload)

        integrator = LedgerIntegrator(the_store)
        ingestor = Ingestor(
            the_store, integrator, on_payload=_on_payload, on_stat=gate.confirm
        )
        app.state.store = the_store
        app.state.bus = bus
        app.state.ingestor = ingestor
        app.state.integrator = integrator
        app.state.gate = gate
        app.state.rules = rules_engine
        app.state.mqtt = None

        def _publish_retained(topic: str, payload: str, retain: bool) -> bool:
            """The fingerprint push's publisher, separate from the gate's.

            `smartwatt/fingerprints` is not a command topic and never goes
            through gate.py. MqttIngest.publish_retained still runs the
            command-topic refusal, so this cannot become a way round the
            gate. Reads `app.state.mqtt` on every call, like
            `_publish_command` above, and reports False rather than raising
            when there is no client -- the push then says it did not publish.
            """
            if not retain:
                raise ValueError("publish_retained publishes retained messages only")
            mqtt_ingest = app.state.mqtt
            if mqtt_ingest is None:
                return False
            return mqtt_ingest.publish_retained(topic, payload)

        app.state.fingerprint_publisher = _publish_retained
        trained = (
            config.trained_classes
            if config.trained_classes is not None
            else catalogue.trained
        )
        app.state.wizard = Wizard(
            the_store, config.fingerprints_path, classes=trained,
            # R21: the push is performed by the Wizard itself, through this
            # same publisher -- never a paho import or a `cmnd`/topic
            # literal added here, so the architecture scan's search stays
            # confined to ingest.py and this file.
            publisher=_publish_retained,
        )

        if start_ingest:
            # 6. Broker: a warning, not a failure. Stored data still serves.
            #
            # The gate's capability is handed over HERE, at construction,
            # and this is the only call to take_command_authority()
            # anywhere -- a second one raises. That ordering is what makes
            # the identity check in publish_command mean anything: the
            # authorised object is fixed before the client is reachable at
            # app.state.mqtt, so there is no window in which something
            # else could authorise itself first.
            app.state.mqtt = MqttIngest(
                config, ingestor, command_authority=gate.take_command_authority()
            )
            app.state.mqtt.start()

        rules_task = asyncio.create_task(_rules_loop(rules_engine))
        retention = asyncio.create_task(_retention_loop(the_store, config))
        try:
            yield
        finally:
            # Both background tasks call into the_store every tick.
            # cancel() only REQUESTS cancellation -- the task stays
            # pending until the loop actually delivers it -- so it is
            # awaited below rather than left to happen on its own. Two
            # real costs otherwise: a task whose cancellation the loop
            # never gets to deliver before tearing down is destroyed
            # still pending, which asyncio reports as an error on its
            # own; and a tick that ran after the_store.close() set the
            # connection to None would raise an AttributeError that the
            # loop's own `except Exception` swallows and re-logs every
            # cycle rather than surfacing. This is NOT the segfault
            # store.py's Store.close() docstring warns about -- that
            # risk is specific to a genuinely separate OS thread racing
            # close(); rules_task and retention are asyncio tasks on
            # this SAME loop as this finally block, so tick() can never
            # be mid-execution while it runs, and close() takes the same
            # lock every writer does regardless.
            rules_task.cancel()
            retention.cancel()
            await asyncio.gather(rules_task, retention, return_exceptions=True)
            if app.state.mqtt is not None:
                app.state.mqtt.stop()
            if owned_store:
                the_store.close()

    app = FastAPI(title="SmartWatt", lifespan=lifespan)

    # -- REST ---------------------------------------------------------------

    @app.get("/api/live")
    def live():
        row = the_store.latest_telemetry()
        if row is None:
            return None
        return {
            "ts": row["ts"],
            "source": row["source"],
            "seq": row["seq"],
            "fingerprint_id": row["fingerprint_id"],
            "electrical": {
                k: row[k] for k in
                ("vrms", "irms", "p", "q1", "dist", "s",
                 "pf_true", "pf_disp", "freq", "range")
            },
            "attribution": {
                "residual_w": row["residual_w"],
                "floor_w": row["floor_w"],
            },
            "energy": {"wh_session": row["wh_session"], "wh_today": row["wh_today"]},
        }

    @app.get("/api/series")
    def series(window: str = Query("15m", pattern=r"^\d+m$")):
        minutes = int(window[:-1])
        now = time.time()
        since = now - minutes * 60
        rows = the_store.telemetry_since(since)
        # +60 pads the end bound into the current, still in-progress minute:
        # ledger_1min rows for "now" exist as soon as a payload lands, but
        # ts_min < int(end_ts // 60) would otherwise exclude that partial
        # minute until it rolls over. Same pad as /api/ledger below.
        ledger_rows = the_store.ledger_minutes(since, now + 60)

        # Sorted so t_min and every series[] list line up positionally by
        # index. A minute with no ledger rows at all is simply absent from
        # t_min -- a gap stays a gap, never interpolated (non-negotiable #2).
        ts_mins = sorted({r["ts_min"] for r in ledger_rows})
        w_mean_by_minute = {
            (r["ts_min"], r["appliance_id"]): r["w_mean"] for r in ledger_rows
        }
        appliance_ids = sorted({r["appliance_id"] for r in ledger_rows})

        return {
            "t": [r["ts"] for r in rows],
            "total_p": [r["p"] for r in rows],
            "t_min": [ts_min * 60 for ts_min in ts_mins],
            "series": {
                # 0.0 here is a measurement, not an invention: the minute
                # itself is real (it's in t_min because SOME appliance had
                # a row in it), and this appliance drawing nothing in it is
                # a fact about that minute, not a gap being papered over.
                appliance_id: [
                    w_mean_by_minute.get((ts_min, appliance_id), 0.0)
                    for ts_min in ts_mins
                ]
                for appliance_id in appliance_ids
            },
        }

    @app.get("/api/events")
    def events(limit: int = Query(50, ge=1, le=500)):
        return the_store.events(limit=limit)

    @app.get("/api/month")
    def month():
        return month_payload(the_store, time.time())

    @app.get("/api/ledger")
    def ledger(window: Literal["today", "week", "month"] = "today"):
        now = time.time()
        # All three windows are LOCAL CALENDAR windows: the day starts at
        # midnight Asia/Kuching, the week on Monday, the month on the 1st.
        # A rolling "today" at 09:00 carried fifteen hours of yesterday --
        # potentially more than doubling the headline today-cost, and
        # contradicting the device's own wh_today in the same payload set.
        # localtime.py exists so that boundary is computed in one place.
        if window == "month":
            start, _ = month_bounds(now)
        elif window == "week":
            start, _ = week_bounds(now)
        else:
            start, _ = day_bounds(now)
        return {"window": window, "rows": the_store.ledger(start, now + 60)}

    @app.get("/api/appliances")
    def appliances():
        return the_store.appliances()

    @app.get("/api/health")
    def health():
        stats = the_store.stats()
        row = the_store.latest_telemetry()
        source = row["source"] if row else None
        sampler = None
        if row and source == "device":
            sampler = {
                "isr_overruns": row["isr_overruns"],
                "worst_isr_us": row["worst_isr_us"],
                "cycles_dropped": row["cycles_dropped"],
            }
        return {
            **stats,
            "broker_connected": bool(
                app.state.mqtt.connected if app.state.mqtt else False
            ),
            "source": source,
            "sampler": sampler,
            "ws_clients": bus.client_count,
            "accumulator_resets": app.state.integrator.resets,
            "device_wh_session": row["wh_session"] if row else None,
            # The rules engine's own liveness. Everything else here
            # reports the INBOUND side -- broker, ingest counters, the
            # sampler -- and a broker that is connected and ingesting says
            # nothing about whether _rules_loop below is still ticking. A
            # tick() that raises every second logs a traceback and
            # otherwise just stops warning and stops acting; this is where
            # that becomes something an operator can query. It carries the
            # freshness of the engine's own observations for the same
            # reason: both are "can this engine still act, and on what".
            "rules": app.state.rules.health(),
        }

    @app.post("/api/whatif")
    def what_if(request: WhatIfRequest):
        try:
            return whatif_payload(
                the_store, request.appliance_id, request.hours, time.time()
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Ruling A: every route below must be registered here, above the SPA
    # catch-all further down. FastAPI matches routes in registration
    # order, so a GET route registered after `spa` is dead -- it would
    # never be reached; `spa` would answer first with index.html (or a
    # 404 for an "api/"-prefixed path, per its own comment below) and
    # this handler would never run at all.

    @app.post("/api/control")
    def control(request: ControlRequest):
        result = app.state.gate.send(
            Command(request.appliance_id, request.command), Actor.USER
        )
        # A refusal is a successful, expected outcome of the safety gate,
        # not a server error. US49 asks an evaluator to ATTEMPT a
        # protected cut and watch it refused, so the attempt must return
        # cleanly with the refusal described.
        return {"outcome": result.outcome.value, "reason": result.reason}

    @app.get("/api/pending")
    def pending():
        now = time.time()
        return [
            {
                "id": p.id,
                "appliance_id": p.appliance_id,
                "rule": p.rule,
                "remaining_s": max(0.0, p.acts_at - now),
            }
            for p in app.state.rules.pending()
        ]

    @app.post("/api/pending/{pending_id}/cancel")
    def cancel(pending_id: str):
        if not app.state.rules.cancel(pending_id):
            raise HTTPException(status_code=404, detail="no such pending cut")
        return {"cancelled": True}

    @app.get("/api/actions")
    def actions(limit: int = Query(100, ge=1, le=1000)):
        # Through Store.actions(), not connection.execute() here: every
        # other reader in this module goes through a Store method, and
        # the ordering itself (ts DESC, rowid DESC -- ts alone ties for
        # a burst of same-tick actions) belongs in one place rather than
        # copied into whichever route happens to need it.
        return the_store.actions(limit)

    @app.get("/api/rules")
    def rules():
        config = app.state.rules.config
        return {
            "left_on_seconds": config.left_on_seconds,
            "standby_band_w": list(config.standby_band_w),
            "standby_seconds": config.standby_seconds,
            "grace_seconds": config.grace_seconds,
            # Surfaced so a demonstration threshold is never mistaken for
            # the shipped default.
            "is_demo": config.is_demo,
            "left_on_targets": list(app.state.rules.config_targets("left_on")),
        }

    # -- setup wizard -------------------------------------------------------

    @app.get("/api/wizard")
    def wizard_state():
        return app.state.wizard.state()

    @app.post("/api/wizard/begin")
    def wizard_begin():
        app.state.wizard.begin()
        return app.state.wizard.state()

    @app.post("/api/wizard/baseline")
    def wizard_baseline():
        # R24: measured from the newest stored telemetry, server-side.
        try:
            app.state.wizard.record_baseline()
        except (NoTelemetry, WrongStep) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return app.state.wizard.state()

    @app.post("/api/wizard/capture")
    def wizard_capture(request: CaptureRequest):
        result = app.state.wizard.capture(
            request.label, request.edge, None, request.conditions.model_dump()
        )
        return {
            "accepted": result.accepted, "reason": result.reason,
            "training_id": result.training_id,
            "captured": result.captured, "required": result.required,
            # R25: what was bound, so a wrong binding looks wrong.
            "event_ts": result.event_ts, "delta_p": result.delta_p,
            "event_reason": result.event_reason,
        }

    @app.post("/api/wizard/confirm")
    def wizard_confirm(request: ConfirmRequest):
        try:
            confirmed = app.state.wizard.confirm(request.training_id)
        except WrongStep as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not confirmed:
            raise HTTPException(status_code=404, detail="no such capture")
        return app.state.wizard.state()

    @app.post("/api/wizard/advance")
    def wizard_advance():
        # R21: the push itself -- build, then publish RETAINED -- happens
        # inside Wizard.advance(), through the same publisher this module
        # already owns (`app.state.fingerprint_publisher`), and is retried
        # by a later call while still in PUSH. R22: publishing is NOT what
        # moves PUSH to VERIFY; the device's newest telemetry reporting
        # the table's fingerprint_id is. advance() returns False only for
        # an unmet requirement (no baseline, unmet quotas, an unreadable
        # file), stated in `refusal`; a push that has not landed is not
        # that kind of refusal, so it is never a 409.
        if not app.state.wizard.advance():
            raise HTTPException(
                status_code=409,
                detail=app.state.wizard.refusal
                or "required captures not complete for this step",
            )
        state = app.state.wizard.state()
        # Compatibility: `published`/`fingerprint_id`/`push_refused` used
        # to live only at the top level of this one response. They are
        # mirrored here from the wizard's own record of the attempt this
        # call just made, which is what still reports them on exactly the
        # call that succeeds and leaves PUSH for VERIFY -- state()'s own
        # `push` field has already gone back to null by then, because it
        # is no longer PUSH's business (see Wizard.state()).
        outcome = app.state.wizard.last_push_result()
        if outcome is not None:
            state["published"] = outcome["published"]
            if outcome["published"]:
                state["fingerprint_id"] = outcome["fingerprint_id"]
            elif outcome["reason"] is not None:
                state["push_refused"] = outcome["reason"]
        return state

    @app.post("/api/wizard/verify")
    def wizard_verify(request: VerifyRequest):
        try:
            return app.state.wizard.verify(request.expected)
        except (WrongStep, NoEventToVerify) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/wizard/scatter")
    def wizard_scatter():
        try:
            rows = app.state.wizard.rows()
        except MalformedFingerprints as exc:
            # R27c: a bad hand edit is stated, not a 500.
            return {"points": [], "labels": [],
                    "error": f"the training file cannot be read: {exc}"}
        if len(rows) < 3:
            return {"points": [], "labels": []}
        X, y = to_matrix(rows)
        projected = PCA(n_components=2, random_state=0).fit_transform(
            StandardScaler().fit_transform(X)
        )
        return {
            "points": [
                {"x": float(px), "y": float(py), "label": str(label)}
                for (px, py), label in zip(projected, y)
            ],
            "labels": sorted(set(str(label) for label in y)),
        }

    # -- WebSocket ----------------------------------------------------------

    @app.websocket("/api/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        subscriber = await bus.subscribe()
        try:
            while True:
                message = await subscriber.get()
                if subscriber.dropped:
                    # The bounded queue overflowed: this client has already
                    # missed messages, so the stream it is being served is
                    # a lie. Disconnect rather than let it render a gapped
                    # chart with no way to know -- S4 reconnects and
                    # refetches (US10). Checked BEFORE the send so nothing
                    # goes out after the gap.
                    log.warning("slow websocket client dropped messages; closing")
                    await ws.close(code=1011)
                    break
                await ws.send_json(message)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(subscriber)

    # -- static -------------------------------------------------------------

    if config.static_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=config.static_dir / "assets", check_dir=False),
            name="assets",
        )

    @app.get("/{path:path}")
    def spa(path: str):
        # Every real route is registered above and matches first; reaching
        # this handler for something under api/ means the client asked for
        # an API path that doesn't exist. 404 there rather than falling
        # through to index.html, or an unknown endpoint would look like a
        # successful page load instead of failing loudly.
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        index = config.static_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(_FALLBACK_INDEX)

    return app


async def _rules_loop(rules: RulesEngine) -> None:
    """Advance the rules engine roughly once a second.

    tick() runs here, on the asyncio loop -- sweeping unconfirmed
    commands, warning, and acting on expired grace periods. observe()
    (fed by lifespan's _on_payload closure above) runs instead on paho's
    ingest thread. RulesEngine's own lock is what makes that split safe
    -- see its _state_lock docstring -- so this loop adds no locking of
    its own; it only ever calls the one method meant to be called from
    here.
    """
    while True:
        try:
            await asyncio.sleep(_RULES_TICK_INTERVAL_S)
            rules.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rules loop error; continuing")


async def _retention_loop(store: Store, config: Settings) -> None:
    """Roll 1 Hz into one-minute buckets, then delete the rolled rows."""
    while True:
        try:
            await asyncio.sleep(config.rollup_interval_s)
            cutoff = time.time() - config.hz_retention_s
            rolled = store.rollup(before_ts=cutoff)
            deleted = store.purge_hz(before_ts=cutoff)
            if rolled or deleted:
                log.info("rollup: %s minutes, purged %s rows", rolled, deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("retention loop error; continuing")


app = create_app()
