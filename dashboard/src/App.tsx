import { useCallback, useEffect, useState } from "react";
import { useLiveSocket } from "./hooks/useLiveSocket";
import {
  getActions,
  getAppliances,
  getLedger,
  getMonth,
  getPending,
  getRules,
  getSeries,
  getWizardScatter,
  getWizardState,
  postCancel,
  postControl,
  postWhatIf,
  postWizardAdvance,
  postWizardBaseline,
  postWizardBegin,
  postWizardCapture,
  postWizardConfirm,
  postWizardVerify,
  type ActionRow,
  type ApplianceRow,
  type CaptureConditions,
  type CaptureResult,
  type ControlResult,
  type LedgerRow,
  type MonthPayload,
  type PendingCut,
  type RulesConfig,
  type ScatterPayload,
  type SeriesPayload,
  type VerifyResult,
  type WhatIfResult,
  type WizardState,
} from "./lib/api";
import { multiplyRinggit } from "./lib/money";
import { ConnectionState } from "./components/ConnectionState";
import { Live } from "./screens/Live/Live";
import { Month } from "./screens/Month/Month";
import { Appliances } from "./screens/Appliances/Appliances";
import { Control } from "./screens/Control/Control";
import { Setup } from "./screens/Setup/Setup";

/**
 * The server's own reason for a refused request (lib/api.ts's ApiError
 * carries FastAPI's `detail`), else the fallback. Duck-typed rather than
 * `instanceof`, so it holds for any error shape that carries a detail.
 */
function refusalOf(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

export type ScreenName = "live" | "month" | "appliances" | "control" | "setup";
type LedgerWindow = "today" | "week" | "month";

const SCREENS: { id: ScreenName; label: string }[] = [
  { id: "live", label: "Live" },
  { id: "month", label: "Month" },
  { id: "appliances", label: "Appliances" },
  { id: "control", label: "Control" },
  { id: "setup", label: "Setup" },
];

const EMPTY_SCATTER: ScatterPayload = { points: [], labels: [] };

const WHATIF_FAILED =
  "That what-if could not be calculated. /api/whatif has no ledger entry " +
  "for this appliance in the current month, or the request failed.";

export function App() {
  const [screen, setScreen] = useState<ScreenName>("live");
  const { telemetry, status, stale } = useLiveSocket();
  // Ruling AD, extended: null means "not available" here too. `/api/series`
  // is a 30 s REST poll; swallowing its failure left the previous 15-minute
  // window on screen indefinitely, rendering an old window as the current
  // one with nothing saying so.
  //
  // Ruling D: SeriesPayload has four fields. `t_min` is the per-minute axis
  // the ledger chart (StackedPower, via Live) is indexed against, and is
  // easy to drop because `t` looks like the only axis on offer -- a payload
  // built without it does not type-check, which is exactly the point.
  const [series, setSeries] = useState<SeriesPayload | null>(null);
  const [month, setMonth] = useState<MonthPayload | null>(null);
  // Ruling AD: null means "not available" (never fetched yet, or the
  // fetch failed) -- distinct from an empty array, which means the fetch
  // SUCCEEDED and the window genuinely has no rows. Collapsing those two
  // into one falsy-ish state is exactly how a failed getLedger used to
  // read as "you used nothing this window" instead of "we don't know".
  const [ledger, setLedger] = useState<LedgerRow[] | null>(null);
  const [ledgerWindow, setLedgerWindow] = useState<LedgerWindow>("today");
  const [appliances, setAppliances] = useState<ApplianceRow[]>([]);
  const [whatIf, setWhatIf] = useState<WhatIfResult | null>(null);
  const [whatIfError, setWhatIfError] = useState<string | null>(null);
  // Polled every 2 s, independent of the 30 s tick below: a pending cut's
  // countdown and a just-issued command's outcome are both time-sensitive
  // in a way the appliance roster and month figures are not. Both default
  // to `[]`, not `null` -- unlike `rulesConfig` below, a failed poll here
  // self-heals within 2 s (the same reasoning Ruling J already applies to
  // `appliances` just below), so there is no meaningful window where
  // showing the previous, still-recent list reads as a confirmed "nothing
  // pending" the way a stale MonthPayload would.
  const [pending, setPending] = useState<PendingCut[]>([]);
  const [actions, setActions] = useState<ActionRow[]>([]);
  // Ruling A: `null` means "we do not know whether the active thresholds
  // are demo or shipped" -- and Control renders NO thresholds panel at
  // all in that case, rather than falling back to a guess. Defaulting
  // `is_demo` to `false` on a failed fetch would be exactly the "small
  // dishonesty" the spec calls out: a demo threshold that silently reads
  // as a shipped default the one time this fetch fails.
  const [rulesConfig, setRulesConfig] = useState<RulesConfig | null>(null);
  // The most recent /api/control result, shown as a banner on the Control
  // screen. `null` before any command has been issued this session.
  const [lastOutcome, setLastOutcome] = useState<ControlResult | null>(null);

  // -- setup wizard (S8) ----------------------------------------------------
  // Fetched only while the Setup screen is showing (see the effect below),
  // not on the 30 s tick above: the other tabs have no use for wizard
  // progress, and gating it this way keeps every OTHER screen's tests
  // untouched by a fetch pair they never exercise.
  const [wizard, setWizard] = useState<WizardState | null>(null);
  const [scatter, setScatter] = useState<ScatterPayload>(EMPTY_SCATTER);
  const [lastCapture, setLastCapture] = useState<CaptureResult | null>(null);
  const [advanceError, setAdvanceError] = useState<string | null>(null);
  const [baselineError, setBaselineError] = useState<string | null>(null);
  const [lastVerify, setLastVerify] = useState<VerifyResult | null>(null);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  // One id per wizard session, generated once when the operator begins --
  // not per capture, so every row of one sitting shares it (Ruling, Task 5
  // dispatch: "conditions with session_id ... per wizard begin").
  const [sessionId, setSessionId] = useState(() => crypto.randomUUID());

  useEffect(() => {
    const tick = () => {
      getSeries("15m").then(setSeries).catch(() => setSeries(null));
      // Ruling AD: an unrecovered failure must not leave a stale
      // MonthPayload on screen forever with no sign anything is wrong --
      // falling back to null explicitly is what lets the render below
      // tell "we have real figures" apart from "we don't".
      getMonth().then(setMonth).catch(() => setMonth(null));
      // Ruling AD: this used to be its own mount-only effect with no
      // retry (Ruling J), so one transient failure -- a startup race with
      // the server is the obvious case -- left protectedIds empty for the
      // rest of the session, indistinguishable from "confirmed nothing is
      // protected". Folding it into the 30 s tick costs nothing (the
      // roster is static in practice) and makes that failure self-heal on
      // the next tick instead.
      getAppliances().then(setAppliances).catch(() => {});
      // Ruling A: same explicit-null-on-failure treatment as getMonth
      // above, and for the same reason -- see rulesConfig's declaration.
      getRules().then(setRulesConfig).catch(() => setRulesConfig(null));
    };
    tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    // Ruling AD: same explicit-null-on-failure treatment as getMonth
    // above, and for the same reason -- a failed fetch must not leave the
    // PREVIOUS window's rows on screen mislabelled as the new window, nor
    // leave the initial `[]` looking like a successfully-loaded empty
    // ledger.
    getLedger(ledgerWindow)
      .then((r) => setLedger(r.rows))
      .catch(() => setLedger(null));
  }, [ledgerWindow]);

  // Brief Step 5 / Ruling C: /api/pending polls every 2 s. /api/actions
  // rides along on the same cadence rather than the 30 s tick above --
  // US49's refusal, and any rule cut, needs to reach the action log fast
  // enough to demonstrate live, not on the appliance roster's schedule.
  const pollControlState = useCallback(() => {
    getPending().then(setPending).catch(() => {});
    getActions().then(setActions).catch(() => {});
  }, []);

  useEffect(() => {
    pollControlState();
    const id = setInterval(pollControlState, 2_000);
    return () => clearInterval(id);
  }, [pollControlState]);

  // Same 2 s cadence as pending/actions above: a capture, confirm or
  // verify happens live while an operator is standing at the bench, and
  // wizard progress needs to catch up to the device on that timescale, not
  // the 30 s tick. Gated on `screen === "setup"` so no other screen's tests
  // ever have to know these two endpoints exist.
  useEffect(() => {
    if (screen !== "setup") return;
    const poll = () => {
      getWizardState().then(setWizard).catch(() => {});
      getWizardScatter().then(setScatter).catch(() => {});
    };
    poll();
    const id = setInterval(poll, 2_000);
    return () => clearInterval(id);
  }, [screen]);

  const onWizardBegin = useCallback(() => {
    setSessionId(crypto.randomUUID());
    setLastCapture(null);
    setLastVerify(null);
    setVerifyError(null);
    setBaselineError(null);
    setAdvanceError(null);
    postWizardBegin().then(setWizard).catch(() => {});
  }, []);

  // R24: the server records the baseline from its own stored telemetry.
  // Nothing is sent, and a refusal (no telemetry yet) is shown.
  const onWizardBaseline = useCallback(() => {
    postWizardBaseline()
      .then((state) => {
        setWizard(state);
        setBaselineError(null);
      })
      .catch((error: unknown) =>
        setBaselineError(
          refusalOf(error, "the baseline request did not reach the server"),
        ),
      );
  }, []);

  const onWizardCapture = useCallback(
    (
      label: string,
      edge: "on" | "off",
      conditions: CaptureConditions,
    ) => {
      // R24: only what the operator knows -- the session id (App-level
      // state) plus Setup's concurrent_ids/notes. background_w, vrms_mean
      // and freq_mean are measured SERVER-side from stored telemetry at the
      // bound event; sending a fallback zero here would present an
      // unmeasured 0 W / 0 V / 0 Hz as a measurement.
      const full: CaptureConditions = { ...conditions, session_id: sessionId };
      postWizardCapture(label, edge, full)
        .then(setLastCapture)
        .catch(() =>
          setLastCapture({
            accepted: false,
            reason: "the capture request did not reach the server",
            training_id: null,
            captured: wizard?.confirmed ?? 0,
            required: wizard?.required ?? 0,
          }),
        );
    },
    [sessionId, wizard],
  );

  const onWizardConfirm = useCallback((trainingId: string) => {
    postWizardConfirm(trainingId)
      .then((state) => {
        setWizard(state);
        setLastCapture(null);
      })
      .catch(() => {});
  }, []);

  const onWizardAdvance = useCallback(() => {
    postWizardAdvance()
      .then((next) => {
        setWizard(next);
        setAdvanceError(null);
      })
      .catch((error: unknown) => {
        // A 409 states its own reason (no baseline, unmet quotas, an
        // unreadable training file) -- shown, never a silent no-op.
        setAdvanceError(
          `Could not advance: ${refusalOf(error, "the request did not reach the server")}`,
        );
      });
  }, []);

  const onWizardVerify = useCallback((expected: string) => {
    // R15: a wrong answer sends the wizard back to "quiet" server-side --
    // the next state this sets IS that reversal, so nothing extra is
    // needed here for the gate to show up: Setup renders whatever step
    // the returned state carries.
    // R27a: the device's answer is shown, and a refusal (409 "no event
    // has arrived") is shown too -- never swallowed.
    postWizardVerify(expected)
      .then((result) => {
        setLastVerify(result);
        setVerifyError(null);
        return getWizardState().then(setWizard);
      })
      .catch((error: unknown) =>
        setVerifyError(
          refusalOf(error, "the verify request did not reach the server"),
        ),
      );
  }, []);

  const onControlCommand = useCallback(
    (applianceId: string, command: "ON" | "OFF") => {
      postControl(applianceId, command)
        .then((result) => {
          setLastOutcome(result);
          // Refetch immediately rather than waiting up to 2 s: US49 asks
          // an evaluator to attempt a protected cut and watch it
          // refused, and the refusal row appearing in the log below is
          // part of that demonstration.
          pollControlState();
        })
        .catch(() => {
          // A transport/server failure, NOT a refusal -- postControl
          // only throws for that (api.py's /api/control returns 200
          // with outcome "REFUSED_..." for an actual refusal, per its
          // own docstring). Nothing was logged server-side in this
          // branch, so staying silent would read as the click having
          // done nothing at all.
          setLastOutcome({
            outcome: "REQUEST_FAILED",
            reason: "the control request did not reach the server",
          });
        });
    },
    [pollControlState],
  );

  const onControlCancel = useCallback(
    (pendingId: string) => {
      postCancel(pendingId)
        .then(pollControlState)
        .catch(() => {
          // 404 (already fired, already cancelled, or never existed) or
          // a transport failure -- either way the next poll (<=2 s)
          // reconciles the pending list with whatever actually happened
          // server-side, so there is nothing further to do here.
        });
    },
    [pollControlState],
  );

  const onWhatIf = useCallback((applianceId: string, hours: number) => {
    postWhatIf(applianceId, hours)
      .then((result) => {
        setWhatIf(result);
        setWhatIfError(null);
      })
      .catch(() => {
        // Clearing the result WITHOUT saying anything made the panel
        // silently vanish, which reads identically to "not calculated
        // yet". The failure is now stated.
        setWhatIf(null);
        setWhatIfError(WHATIF_FAILED);
      });
  }, []);

  // Ruling J: US38's un-cuttable mark must be reachable in the running
  // product, not just in a test that hands Appliances a hardcoded Set --
  // so it is built here from the server's `protected` column (SQLite
  // INTEGER 0/1) rather than wired as `new Set()`.
  const protectedIds = new Set(
    appliances.filter((a) => Boolean(a.protected)).map((a) => a.id),
  );

  // Ruling B: Control gets only devices with a REGISTERED PLUG. desk_fan
  // is seeded with plug_device: null (registry.py's _DEFAULTS), so a
  // toggle for it would only ever issue a command the gate refuses as
  // REFUSED_UNREGISTERED -- a control that can never do anything, and a
  // refusal that demonstrates missing hardware rather than the safety
  // gate. This does NOT extend to laptop_charger: it is PROTECTED but
  // DOES have a plug, so it is not filtered here -- an evaluator
  // attempting that cut and watching it refused is US49, the
  // demonstration this whole sub-project exists for.
  //
  // Ruling D: /api/appliances (ApplianceRow) is `id`/`0|1`; Control's own
  // Device shape is `appliance_id`/boolean flags. Converted here, not by
  // changing ApplianceRow itself -- its own doc comment (lib/api.ts)
  // explains why that integer typing stays exactly as SQLite sends it,
  // and this same `appliances` array already has two other direct
  // consumers: `protectedIds` immediately above (reads `.protected` as
  // that raw 0/1) and Month's what-if picker (reads `.id`/`.display_name`
  // only). Widening ApplianceRow itself would touch both of those, which
  // is a larger, separate change than this task's scope.
  const controlDevices = appliances
    .filter((a) => a.plug_device !== null)
    .map((a) => ({
      appliance_id: a.id,
      display_name: a.display_name,
      plug_device: a.plug_device,
      protected: Boolean(a.protected),
      heating: Boolean(a.heating),
    }));

  // Marginal cost of the next kWh, scaled to the current draw. US3: the
  // burn rate uses the MARGINAL rate, never the nominal one.
  //
  // `null` when /api/month is unavailable, and deliberately NOT `"0.00"`.
  // The staleness clock only tracks the WebSocket, so with a healthy
  // socket and a failing /api/month the substituted zero rendered as a
  // fresh, un-staled "Cost per hour RM 0.00" -- an invented figure
  // asserted as current fact.
  const marginal = month?.cliff.marginal ?? null;
  const kwPerHour = (telemetry?.electrical.p ?? 0) / 1000;
  // Ruling E: money is parsed exactly from its string and never becomes a
  // float, even transiently. multiplyRinggit does rate x quantity in
  // BigInt; `(Number(marginal) * kwPerHour).toFixed(4)` is exactly the
  // move it exists to replace.
  const costPerHour =
    marginal === null ? null : multiplyRinggit(marginal, kwPerHour);
  // Same treatment for the carbon factor: no factor, no figure. A
  // published emission factor is a physical quantity, so Number() on it
  // is correct -- but only once it is known to exist.
  const carbonFactor = month?.carbon.local.factor_kg_per_kwh ?? null;
  const gramsPerHour =
    carbonFactor === null ? null : kwPerHour * Number(carbonFactor) * 1000;

  return (
    <div className="mx-auto max-w-5xl px-8 py-6">
      <header className="rule-strong flex items-baseline justify-between pb-3">
        <h1 className="text-lg font-semibold tracking-tight">SmartWatt</h1>
        <div className="flex items-baseline gap-6">
          <nav className="flex gap-6">
            {SCREENS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setScreen(entry.id)}
                aria-current={screen === entry.id ? "page" : undefined}
                className={
                  screen === entry.id
                    ? "text-ink border-ink border-b-2 pb-1 text-sm"
                    : "text-ink-muted pb-1 text-sm"
                }
              >
                {entry.label}
              </button>
            ))}
          </nav>
          {/* Spec, Data flow: one socket hook owns connection state and
              EVERY screen reads it. Living in the header is what makes
              that true -- Month used to show a confident bill, projection
              and Cliff Gauge with nothing saying ingestion had stopped
              three days ago. This is the only ConnectionState in the app;
              Live no longer renders its own. */}
          <ConnectionState status={status} stale={stale} />
        </div>
      </header>

      <main className="pt-6">
        {screen === "live" && (
          <Live
            telemetry={telemetry}
            stale={stale}
            series={series}
            marginalRinggitPerHour={costPerHour}
            gramsCo2PerHour={gramsPerHour}
          />
        )}
        {screen === "month" &&
          (month ? (
            <Month
              month={month}
              onWhatIf={onWhatIf}
              appliances={appliances}
              whatIf={whatIf}
              whatIfError={whatIfError}
            />
          ) : (
            // Ruling AD: Live already has this fallback ("Waiting for
            // telemetry..."); Month never got one, so a failed or
            // not-yet-completed getMonth() used to render nothing at all
            // here -- a blank tab under the header reads as broken, not
            // as "not loaded yet".
            <div
              data-testid="month-unavailable"
              className="text-ink-muted py-16 text-center text-sm"
            >
              Month figures are not available right now.
            </div>
          ))}
        {screen === "appliances" &&
          (ledger !== null ? (
            <Appliances
              rows={ledger}
              window={ledgerWindow}
              onWindowChange={setLedgerWindow}
              protectedIds={protectedIds}
              // NOT a rate. Each row is priced as its share of this real
              // bill: under flat-band Tariff D one rate applies to every
              // unit in the month, so an appliance's share of the bill is
              // what that appliance cost. See Appliances.tsx for why
              // cliff.marginal -- a difference of two whole bills -- must
              // never be used here.
              monthBillTotalRinggit={month?.bill.total ?? null}
              monthBillKwh={month?.bill.kwh ?? null}
              kgCo2PerKwh={month?.carbon.local.factor_kg_per_kwh ?? null}
            />
          ) : (
            // Ruling AD: Appliances itself is blameless -- it faithfully
            // renders whatever `rows` it's given, residual included. But
            // a failed getLedger used to leave `rows` at its initial `[]`,
            // which Appliances renders as an honest-looking empty table
            // (no residual row possible from zero rows either) -- reading
            // as "you used nothing this window" rather than "unavailable".
            <div
              data-testid="appliances-unavailable"
              className="text-ink-muted py-16 text-center text-sm"
            >
              Appliance data is not available right now.
            </div>
          ))}
        {screen === "control" && (
          <Control
            devices={controlDevices}
            pending={pending}
            actions={actions}
            onCommand={onControlCommand}
            onCancel={onControlCancel}
            lastOutcome={lastOutcome}
            rules={rulesConfig}
          />
        )}
        {screen === "setup" &&
          (wizard ? (
            <Setup
              state={wizard}
              scatter={scatter}
              onBegin={onWizardBegin}
              onRecordBaseline={onWizardBaseline}
              onCapture={onWizardCapture}
              onConfirm={onWizardConfirm}
              onAdvance={onWizardAdvance}
              onVerify={onWizardVerify}
              lastCapture={lastCapture}
              advanceError={advanceError}
              baselineError={baselineError}
              lastVerify={lastVerify}
              verifyError={verifyError}
            />
          ) : (
            <div
              data-testid="setup-unavailable"
              className="text-ink-muted py-16 text-center text-sm"
            >
              Wizard state is not available right now.
            </div>
          ))}
      </main>
    </div>
  );
}
