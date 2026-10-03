import type {
  SmartWattEvent,
  TelemetryElectrical,
  TelemetryEnergy,
} from "../types/contract";

/**
 * Same-origin relative paths only. The dashboard is served by the API, so
 * there is no base URL to configure and nothing resolves a hostname.
 */
async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return (await response.json()) as T;
}

/**
 * `/api/live` (api.py:95-114) returns a PROJECTION of the stored row, not
 * a Telemetry: no `schema`, no `attribution.active`, no `health`. A
 * dedicated type says so, rather than `Telemetry | null` claiming three
 * fields that are never actually present on this endpoint.
 */
export interface LiveSnapshot {
  ts: number;
  source: "device" | "simulator" | "replay";
  seq: number;
  fingerprint_id: string | null;
  electrical: TelemetryElectrical;
  attribution: { residual_w: number; floor_w: number };
  energy: TelemetryEnergy;
}

/**
 * Exactly the six keys `Bill.display()` emits
 * (`tariff/smartwatt_tariff/bill.py:37-46`), all of them strings.
 *
 * Typed precisely rather than as `Record<string, string>`, which under
 * `noUncheckedIndexedAccess` made every read `string | undefined` and so
 * needed a `!` at each call site. Those assertions rested on an
 * unenforced cross-language contract: `formatRinggit` throws on
 * `undefined` and there is no error boundary, so a renamed server key
 * would white-screen the whole dashboard instead of degrading. With the
 * keys declared, a rename is a type error in CI rather than a blank page
 * at the demonstration.
 *
 * `kwh` and `rate_sen` are `str(Decimal)` -- physical quantity and
 * published rate. `energy`/`service_tax`/`discount`/`total` are money,
 * rounded to sen by `to_2dp`, and never become numbers here.
 */
export interface BillDisplay {
  kwh: string;
  rate_sen: string;
  energy: string;
  service_tax: string;
  discount: string;
  total: string;
}

export interface MonthPayload {
  now_ts: number;
  mtd_kwh: number;
  projected_kwh: number;
  bill: BillDisplay;
  bill_projected: BillDisplay;
  bill_undiscounted: BillDisplay;
  discount_active: boolean;
  cliff: {
    boundary_kwh: string | null;
    kwh_remaining: string | null;
    current_rate_sen: string | null;
    next_rate_sen: string | null;
    band_step: string;
    marginal: string;
    culprit: {
      appliance_id: string;
      projected_kwh: string;
      sufficient_alone: boolean;
    } | null;
  };
  carbon: {
    local: CarbonFraming;
    hypothetical: CarbonFraming;
    comparison: {
      ratio: string;
      trend: Record<string, string>;
      statement: string;
      displaced_framing: string;
    };
    tree_years: string | null;
  };
}

export interface CarbonFraming {
  region: string;
  kg: string;
  hypothetical: boolean;
  provisional: boolean;
  factor_kg_per_kwh: string;
  provenance: { source: string; url: string; vintage: string };
}

export interface LedgerRow {
  appliance_id: string;
  wh: number;
  w_mean: number;
  minutes: number;
}

export interface ApplianceRow {
  id: string;
  display_name: string;
  /** SQLite INTEGER: 0 or 1. US38's un-cuttable mark keys off this. */
  protected: number;
  heating: number;
  plug_device: string | null;
}

/** The rules engine's own liveness, from `/api/health`.
 *
 *  Every duration is an AGE in seconds, never a timestamp, so nothing on
 *  this side has to know which epoch the server is using.
 *  `telemetry_age_s` and `last_tick_age_s` are null only when the thing
 *  they measure has never happened at all. */
export interface RulesHealth {
  ticks: number;
  /** Cumulative, never reset -- an intermittent failure still shows here
   *  after a later successful tick clears `last_error`. */
  tick_errors: number;
  last_tick_age_s: number | null;
  last_error: string | null;
  telemetry_age_s: number | null;
  max_observation_age_s: number;
  /** False also when nothing has ever been observed. The engine takes no
   *  action on stale observations, so a pending countdown cannot outlive
   *  the evidence for it. */
  observations_fresh: boolean;
  pending_cuts: number;
}

export interface HealthPayload {
  accepted: number;
  rejected_schema: number;
  seq_gaps: number;
  broker_connected: boolean;
  source: string | null;
  sampler: Record<string, number> | null;
  ws_clients: number;
  accumulator_resets: number;
  device_wh_session: number | null;
  rules: RulesHealth;
}

export interface SeriesPayload {
  t: number[];
  total_p: number[];
  /** The per-minute axis `series` is indexed by. NOT the same axis as `t`:
   *  `t`/`total_p` are 1 Hz samples, `series` is the one-minute ledger. */
  t_min: number[];
  series: Record<string, number[]>;
}

export const getLive = () => get<LiveSnapshot | null>("/api/live");
export const getSeries = (window = "15m") =>
  get<SeriesPayload>(`/api/series?window=${window}`);
export const getMonth = () => get<MonthPayload>("/api/month");
export const getEvents = (limit = 50) =>
  get<SmartWattEvent[]>(`/api/events?limit=${limit}`);
export const getHealth = () => get<HealthPayload>("/api/health");
export const getLedger = (window: "today" | "week" | "month") =>
  get<{ window: string; rows: LedgerRow[] }>(`/api/ledger?window=${window}`);
export const getAppliances = () => get<ApplianceRow[]>("/api/appliances");

/**
 * `/api/pending` (api.py's `pending()` route): the rules engine's
 * warn-then-act grace period, in flight. `remaining_s` is computed
 * server-side from the pending cut's own deadline against the request's
 * arrival time, and is never negative -- the route clamps it with
 * `max(0.0, ...)`.
 */
export interface PendingCut {
  id: string;
  appliance_id: string;
  rule: string;
  remaining_s: number;
}
export const getPending = () => get<PendingCut[]>("/api/pending");

/**
 * One row of `/api/actions` -- the `actions` table verbatim, via
 * `Store.actions()` (store.py). `outcome` is one of gate.py's eight
 * `Outcome` values, kept as `string` here rather than a literal union:
 * the dashboard only ever pattern-matches on the `"REFUSED"` prefix, and
 * gate.py's `Outcome` enum is the one source of truth for that closed
 * vocabulary -- this file has no business re-declaring all eight by hand.
 */
export interface ActionRow {
  ts: number;
  actor: string;
  device: string;
  command: string;
  rule: string | null;
  outcome: string;
  reason: string | null;
}
export const getActions = (limit = 100) =>
  get<ActionRow[]>(`/api/actions?limit=${limit}`);

/**
 * `/api/rules`: the ACTIVE thresholds -- `is_demo` is why this endpoint
 * exists at all. A demo threshold (rules.py's `DEMO_DEFAULTS`,
 * `left_on_seconds: 30`) shown with nothing marking it as such would be
 * indistinguishable on screen from the shipped 4-hour default -- exactly
 * the "small dishonesty" non-negotiable #2 rules out. Never defaulted or
 * inferred client-side: Control renders no thresholds panel at all when
 * this is `null`, rather than guessing `is_demo`.
 *
 * `standby_band_w` is a fixed-length pair, not `number[]`: with
 * `noUncheckedIndexedAccess` on, a plain array's `[0]`/`[1]` would each
 * be `number | undefined`, when the server always sends exactly two.
 */
export interface RulesConfig {
  left_on_seconds: number;
  standby_band_w: [number, number];
  standby_seconds: number;
  grace_seconds: number;
  is_demo: boolean;
  left_on_targets: string[];
}
export const getRules = () => get<RulesConfig>("/api/rules");

/** `whatif_payload()` (server/smartwatt_server/month.py:212-220). */
export interface WhatIfResult {
  appliance_id: string;
  hours: number;
  kwh_saved: string;
  naive_saving: string;
  true_saving: string;
  crosses_back: boolean;
  /** A dimensionless ratio, not money: true_saving / naive_saving. */
  multiple: string;
}

export async function postWhatIf(
  appliance_id: string,
  hours: number,
): Promise<WhatIfResult> {
  const response = await fetch("/api/whatif", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ appliance_id, hours }),
  });
  // `/api/whatif` 404s for an appliance id with no ledger entry this
  // month (month.py:197-198 raises KeyError). The caller must surface
  // that, not swallow it -- a panel that silently vanishes is
  // indistinguishable from "not calculated yet".
  if (!response.ok) throw new Error(`whatif: ${response.status}`);
  return (await response.json()) as WhatIfResult;
}

/**
 * `/api/control` (api.py's `control()` route). A refusal is encoded IN
 * the 200 response body, not an HTTP error: `outcome` may be one of
 * gate.py's `REFUSED_*` values, and that is a SUCCESSFUL, expected call
 * -- US49 asks an evaluator to ATTEMPT a protected cut and see it
 * refused, so the attempt must return cleanly with the refusal
 * described. This only throws for a genuine transport/server failure,
 * which App.tsx's caller handles differently (see its onCommand catch).
 */
export interface ControlResult {
  outcome: string;
  reason: string | null;
}

export async function postControl(
  appliance_id: string,
  command: "ON" | "OFF",
): Promise<ControlResult> {
  const response = await fetch("/api/control", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ appliance_id, command }),
  });
  if (!response.ok) throw new Error(`control: ${response.status}`);
  return (await response.json()) as ControlResult;
}

/**
 * `/api/pending/{id}/cancel`. `{cancelled: false}` is never a response
 * the server sends -- it either cancels and returns `{cancelled: true}`,
 * or the id is unknown (already fired, already cancelled, or never
 * existed) and the route 404s instead. The caller treats a 404 the same
 * as a transport failure: either way, the next `/api/pending` poll is
 * what reconciles the UI with reality.
 */
export async function postCancel(
  pendingId: string,
): Promise<{ cancelled: boolean }> {
  const response = await fetch(
    `/api/pending/${encodeURIComponent(pendingId)}/cancel`,
    { method: "POST" },
  );
  if (!response.ok) throw new Error(`cancel: ${response.status}`);
  return (await response.json()) as { cancelled: boolean };
}

// -- setup wizard (S8) ------------------------------------------------------

/** Confirmed rows per edge, for one trained class -- `wizard.py`'s `_breakdown`. */
export interface WizardEdgeCounts {
  on: number;
  off: number;
}

/** `wizard.py: Wizard.state()`'s `breakdown`: one table per capture step,
 *  keyed by trained class. Per (class, edge), not per class, so ten ON
 *  captures cannot stand in for a missing OFF edge. */
export interface WizardBreakdown {
  quiet: Record<string, WizardEdgeCounts>;
  overlapped: Record<string, WizardEdgeCounts>;
}

/**
 * R21: the last push attempt for the CURRENT time the wizard is in the
 * "push" step -- `null` before any attempt, and `null` again once the
 * wizard has left "push" (`wizard.py: Wizard.state()`'s own `push`
 * field). `published` is `true` only when the broker client accepted the
 * retained message; no client, a refused publish, or a table the device
 * could not load all read `false`, never success. This is the field the
 * Setup screen renders from -- NOT the top-level `published`/
 * `fingerprint_id`/`push_refused` keys below, which only ever appear on
 * the single `POST /api/wizard/advance` response that produced them and
 * vanish on the very next poll of `GET /api/wizard`.
 */
export interface WizardPushOutcome {
  published: boolean;
  fingerprint_id: string | null;
  reason: string | null;
}

/**
 * `GET /api/wizard` (`wizard.py: Wizard.state()`), widened with the fields
 * `POST /api/wizard/advance` adds on top of that same shape as a
 * compatibility mirror of `push` (api.py's `wizard_advance` route):
 * `published`/`fingerprint_id`/`push_refused` are present ONLY on that one
 * response, including on the call that both publishes and leaves "push"
 * for "verify" (where `push` itself has already gone back to `null`).
 * `breakdown` is optional here because it is real but not part of every
 * fixture this type is asked to describe.
 */
export interface WizardState {
  step: string;
  confirmed: number;
  required: number;
  per_edge_required?: number;
  breakdown?: WizardBreakdown;
  awaiting_confirmation: number;
  streak: number;
  streak_required: number;
  baseline_w: number | null;
  fingerprints_path: string;
  classes: string[];
  push?: WizardPushOutcome | null;
  /**
   * R22: the id of the table the server built from the training file, and
   * the id the device's NEWEST telemetry reports. PUSH moves to VERIFY only
   * when they are equal -- the broker accepting the retained publish is not
   * the gate. Both null outside "push"/"verify"; the device's is null until
   * it reports one.
   */
  expected_fingerprint_id?: string | null;
  device_fingerprint_id?: string | null;
  /** R27c: the training file cannot be read (a bad hand edit). */
  file_error?: string | null;
  published?: boolean;
  push_refused?: string;
  fingerprint_id?: string;
}

export interface ScatterPoint {
  x: number;
  y: number;
  label: string;
}

export interface ScatterPayload {
  points: ScatterPoint[];
  labels: string[];
  /** R27c: present only when the training file cannot be read. */
  error?: string;
}

/** `POST /api/wizard/capture`'s response (api.py's `wizard_capture`). */
export interface CaptureResult {
  accepted: boolean;
  reason: string | null;
  training_id: string | null;
  captured: number;
  required: number;
  /**
   * R25: the event the server bound (or refused) -- its time, its delta P
   * and the tracker's reason (null for a clean, classified edge) -- so a
   * wrong binding looks different from a right one. Null when nothing was
   * bound at all.
   */
  event_ts?: number | null;
  delta_p?: number | null;
  event_reason?: string | null;
}

/** `POST /api/wizard/verify`'s response (`wizard.py: Wizard.verify()`). */
export interface VerifyResult {
  expected: string;
  actual: string;
  correct: boolean;
  streak: number;
  required: number;
  passed: boolean;
}

export const getWizardState = () => get<WizardState>("/api/wizard");
export const getWizardScatter = () => get<ScatterPayload>("/api/wizard/scatter");

/**
 * A refused request, carrying the server's own `detail` when it sent one
 * (FastAPI's HTTPException body), so a 409's reason can be SHOWN rather
 * than swallowed.
 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string | null,
    path: string,
  ) {
    super(detail ?? `${path}: ${status}`);
    this.name = "ApiError";
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail: string | null = null;
    try {
      const parsed = (await response.json()) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // No JSON body: the status alone is all there is to say.
    }
    throw new ApiError(response.status, detail, path);
  }
  return (await response.json()) as T;
}

export const postWizardBegin = () => postJson<WizardState>("/api/wizard/begin", {});

/**
 * R24: records the baseline SERVER-side, from the newest stored telemetry's
 * total real power. Nothing is sent: there is no typed or defaulted figure.
 * 409 (an ApiError with the reason) when no telemetry has been stored.
 */
export const postWizardBaseline = () =>
  postJson<WizardState>("/api/wizard/baseline", {});

/**
 * R24: everything a capture's conditions can be told by the client. The
 * measured conditions (background_w, vrms_mean, freq_mean) are filled in by
 * the server from stored telemetry, and are deliberately not in this type.
 */
export interface CaptureConditions {
  session_id?: string;
  concurrent_ids?: string;
  notes?: string;
}

/**
 * `event` is deliberately never sent: the server binds the capture to the
 * device's own newest matching edge (`wizard.py`'s `_bind_event`), and
 * answers `accepted: false, reason: "no edge detected"` when there is none.
 */
export const postWizardCapture = (
  label: string,
  edge: "on" | "off",
  conditions: CaptureConditions,
) => postJson<CaptureResult>("/api/wizard/capture", { label, edge, conditions });

export const postWizardConfirm = (training_id: string) =>
  postJson<WizardState>("/api/wizard/confirm", { training_id });

export const postWizardAdvance = () => postJson<WizardState>("/api/wizard/advance", {});

/**
 * `actual` is deliberately never sent: the server reads the device's own
 * newest event since the last verification round (`wizard.py`'s
 * `_read_answer`), which is the whole point of the verify step -- it is
 * checking the DEVICE's classification, not the operator's.
 */
export const postWizardVerify = (expected: string) =>
  postJson<VerifyResult>("/api/wizard/verify", { expected });
