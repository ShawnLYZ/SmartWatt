import { useState } from "react";
import type {
  CaptureConditions,
  CaptureResult,
  ScatterPayload,
  VerifyResult,
  WizardState,
} from "../../lib/api";
import { formatClock } from "../../lib/time";
import { SignatureScatter } from "./SignatureScatter";

const EDGES: Array<"on" | "off"> = ["on", "off"];

const STEP_INSTRUCTIONS: Record<string, string> = {
  baseline: "Everything off. Record the quiet-circuit level, then begin.",
  quiet: "Switch each class on its own, nothing else running. Confirm every capture.",
  overlapped: "Confirm every capture.",
  push: "Get the trained table onto the device. This step ends when the device reports the table's id.",
  verify: "Switch a trained load and say what you expect it to be classified as.",
  done: "Setup is complete.",
}

export interface SetupProps {
  state: WizardState;
  scatter: ScatterPayload;
  onBegin: () => void;
  /** R24: asks the server to record the baseline from its own telemetry. */
  onRecordBaseline?: () => void;
  /**
   * R24: only what the operator knows. The measured conditions come from
   * the server's stored telemetry, never from this screen.
   */
  onCapture: (label: string, edge: "on" | "off", conditions: CaptureConditions) => void;
  onConfirm: (trainingId: string) => void;
  onAdvance: () => void;
  onVerify: (expected: string) => void;
  lastCapture: CaptureResult | null;
  /**
   * `POST /api/wizard/advance` 409s when the current step's requirement is
   * unmet (no baseline, quotas, an unreadable file) -- stated here, never a
   * silent no-op. A push that merely has not landed is a 200, shown from
   * `state.push` and the fingerprint ids below.
   */
  advanceError?: string | null;
  /** R24: why the baseline could not be recorded (e.g. no telemetry). */
  baselineError?: string | null;
  /** R27a: the device's answer to the last verify. */
  lastVerify?: VerifyResult | null;
  /** R27a: a verify refusal (409 "no event yet"), shown, not swallowed. */
  verifyError?: string | null;
}

/** "clean edge" for a classified edge; otherwise the tracker's own reason. */
function edgeState(reason: string | null | undefined): string {
  return reason ? reason : "clean edge";
}

function EventDetail({ capture }: { capture: CaptureResult }) {
  if (capture.event_ts == null) return null;
  return (
    <p data-testid="bound-event" className="figure pt-1 text-xs">
      Bound event: {capture.delta_p == null ? "—" : `${capture.delta_p} W`} ·{" "}
      {edgeState(capture.event_reason)} · {formatClock(capture.event_ts)}
    </p>
  );
}

export function Setup({
  state, scatter, onBegin, onRecordBaseline, onCapture, onConfirm, onAdvance,
  onVerify, lastCapture, advanceError, baselineError, lastVerify, verifyError,
}: SetupProps) {
  const [concurrentIds, setConcurrentIds] = useState("");
  const [notes, setNotes] = useState("");
  const [expected, setExpected] = useState(state.classes[0] ?? "");

  const isCapturing = state.step === "quiet" || state.step === "overlapped";
  const isOverlapped = state.step === "overlapped";
  const showsIds = state.step === "push" || state.step === "verify";
  const expectedId = state.expected_fingerprint_id ?? null;
  const deviceId = state.device_fingerprint_id ?? null;
  const idsMatch = expectedId !== null && deviceId === expectedId;
  // R24: the server refuses to leave BASELINE without a recorded baseline;
  // the button says so up front rather than inviting a refusal.
  const nextBlocked = state.step === "baseline" && state.baseline_w === null;

  const capture = (label: string, edge: "on" | "off") => {
    const conditions: CaptureConditions = {};
    if (isOverlapped) conditions.concurrent_ids = concurrentIds;
    if (notes) conditions.notes = notes;
    onCapture(label, edge, conditions);
  };

  return (
    <section className="space-y-8">
      <div className="flex items-baseline justify-between">
        <h2
          data-testid="wizard-step"
          className="text-sm font-semibold uppercase tracking-wide"
        >
          Step: {state.step}
        </h2>
        {state.step === "baseline" && (
          <span className="flex gap-2">
            <button
              type="button"
              onClick={onBegin}
              className="border-ink border px-3 py-1 text-xs"
            >
              Begin
            </button>
            <button
              type="button"
              onClick={onRecordBaseline}
              className="border-ink border px-3 py-1 text-xs"
            >
              Record baseline
            </button>
          </span>
        )}
      </div>
      <p className="text-ink-muted text-sm">
        {STEP_INSTRUCTIONS[state.step] ?? ""}
      </p>

      {state.file_error && (
        <div data-testid="file-error" className="border-warn text-warn border p-3 text-sm">
          The training file cannot be used until it is fixed: {state.file_error}
        </div>
      )}

      <dl className="grid grid-cols-3 gap-4 text-xs">
        <div>
          <dt className="text-ink-muted uppercase tracking-wide">Progress</dt>
          <dd data-testid="wizard-progress" className="figure">
            {state.confirmed} / {state.required} confirmed
          </dd>
        </div>
        <div>
          <dt className="text-ink-muted uppercase tracking-wide">Baseline</dt>
          <dd data-testid="baseline" className="figure">
            {state.baseline_w === null ? "not recorded" : `${state.baseline_w} W`}
          </dd>
        </div>
        <div>
          <dt className="text-ink-muted uppercase tracking-wide">
            Fingerprints file
          </dt>
          <dd data-testid="fingerprints-path" className="figure break-all">
            {state.fingerprints_path}
          </dd>
        </div>
      </dl>

      {state.step === "baseline" && baselineError && (
        <div data-testid="baseline-refused" className="border-warn text-warn border p-3 text-sm">
          Baseline not recorded — {baselineError}
        </div>
      )}

      {/* R5: the per-class/edge breakdown, in addition to the raw totals
          above -- ten ON captures of one class must not read as though
          they satisfy its OFF edge too. */}
      {state.breakdown && isCapturing && (
        <table className="w-full text-xs">
          <thead>
            <tr className="rule-b text-ink-muted text-left uppercase tracking-wide">
              <th scope="col" className="py-1 font-normal">Class</th>
              <th scope="col" className="py-1 font-normal">On</th>
              <th scope="col" className="py-1 font-normal">Off</th>
            </tr>
          </thead>
          <tbody>
            {state.classes.map((label) => {
              const row = state.breakdown?.[state.step as "quiet" | "overlapped"]?.[label];
              return (
                <tr key={label} className="rule-b">
                  <td className="py-1">{label}</td>
                  <td className="figure py-1">
                    {row?.on ?? 0} / {state.per_edge_required ?? "—"}
                  </td>
                  <td className="figure py-1">
                    {row?.off ?? 0} / {state.per_edge_required ?? "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {isOverlapped && (
        <div className="border-warn border p-3 text-sm">
          <p>
            This step needs another appliance already running -- name it
            below before capturing.
          </p>
          <label className="mt-2 flex flex-col gap-1 text-xs">
            Concurrent appliance(s)
            <input
              type="text"
              value={concurrentIds}
              onChange={(event) => setConcurrentIds(event.target.value)}
              placeholder="desk_fan|led_bulb"
              className="border-rule border px-2 py-1"
            />
          </label>
        </div>
      )}

      {isCapturing && (
        <div>
          <h3 className="rule-b pb-2 text-sm font-semibold">Capture</h3>
          <label className="mb-3 flex flex-col gap-1 text-xs">
            Notes
            <input
              type="text"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              className="border-rule border px-2 py-1"
            />
          </label>
          {state.classes.map((label) => (
            <div
              key={label}
              data-testid={`capture-row-${label}`}
              className="rule-b flex items-center justify-between py-2"
            >
              <span className="text-sm">{label}</span>
              <span className="flex gap-2">
                {EDGES.map((edge) => (
                  <button
                    key={edge}
                    type="button"
                    onClick={() => capture(label, edge)}
                    className="border-ink border px-3 py-1 text-xs"
                  >
                    Capture {edge.toUpperCase()}
                  </button>
                ))}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* R26: a capture is confirmable only in the capture step it was made
          in -- the server drops unconfirmed captures on leaving it, so the
          prompt goes too. */}
      {isCapturing && lastCapture && lastCapture.accepted && (
        <div data-testid="confirm-prompt" className="border-rule border p-3 text-sm">
          <p>
            Captured <strong>{lastCapture.training_id}</strong>. Check the
            event below is the switch you just made, then confirm it before
            it counts.
          </p>
          <EventDetail capture={lastCapture} />
          <button
            type="button"
            onClick={() => onConfirm(lastCapture.training_id as string)}
            className="border-ink mt-2 border px-3 py-1 text-xs"
          >
            Confirm capture
          </button>
        </div>
      )}

      {isCapturing && lastCapture && !lastCapture.accepted && (
        <div
          data-testid="capture-failed"
          className="border-warn text-warn border p-3 text-sm"
        >
          Capture failed — {lastCapture.reason}
          <EventDetail capture={lastCapture} />
        </div>
      )}

      {/* R22: the device's own report is the gate out of "push". */}
      {showsIds && (
        <div className="border-rule border p-3 text-sm">
          <p data-testid="fingerprint-ids" className="figure">
            Table id: device reports {deviceId ?? "nothing yet"}, expected{" "}
            {expectedId ?? "— (no table can be built)"}
          </p>
          {state.step === "push" && !idsMatch && (
            <p data-testid="uploadfs-hint" className="text-warn pt-1">
              Until these match the device is not running this table. It has
              no fingerprint subscriber yet, so the MQTT push cannot load it:
              upload the file with <code>pio run -e esp32-s3 -t uploadfs</code>{" "}
              from firmware/ (the wizard writes firmware/data/fingerprints.csv
              by default), let the device restart, then press Next step.
            </p>
          )}
        </div>
      )}

      {state.step === "verify" && (
        <div className="border-rule border p-3 text-sm">
          <p data-testid="verify-streak">
            Streak: {state.streak} / {state.streak_required}
          </p>
          <p className="text-warn pt-1">
            Setup is not complete until verification passes.
          </p>
          <label className="mt-2 flex flex-col gap-1 text-xs">
            Expected class
            <select
              value={expected}
              onChange={(event) => setExpected(event.target.value)}
              className="border-rule border px-2 py-1"
            >
              {state.classes.map((label) => (
                <option key={label} value={label}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => onVerify(expected)}
            className="border-ink mt-2 border px-3 py-1 text-xs"
          >
            Verify
          </button>
        </div>
      )}

      {lastVerify && (
        <div
          data-testid="verify-result"
          className={
            lastVerify.correct
              ? "border-rule border p-3 text-sm"
              : "border-warn text-warn border p-3 text-sm"
          }
        >
          Verify: expected {lastVerify.expected}, device answered{" "}
          {lastVerify.actual} — {lastVerify.correct ? "correct" : "wrong, back to capture"}
          {lastVerify.passed ? " — verification passed" : ""}
        </div>
      )}

      {state.step === "verify" && verifyError && (
        <div data-testid="verify-refused" className="border-warn text-warn border p-3 text-sm">
          Not verified — {verifyError}
        </div>
      )}

      {/* R21: rendered from `state.push` (the wizard's own persisted
          record). Shown as what it is: whether the broker took the
          retained message -- not whether the device loaded it. */}
      {state.step === "push" && state.push && (
        <div
          data-testid="push-result"
          className={
            state.push.published
              ? "border-rule border p-3 text-sm"
              : "border-warn text-warn border p-3 text-sm"
          }
        >
          {state.push.published
            ? `Published to the broker. fingerprint_id: ${state.push.fingerprint_id}`
            : `Not published — ${state.push.reason ?? "the push was refused"}`}
        </div>
      )}

      {scatter.error && (
        <div data-testid="scatter-error" className="border-warn text-warn border p-3 text-sm">
          Signature scatter unavailable — {scatter.error}
        </div>
      )}

      <SignatureScatter points={scatter.points} labels={scatter.labels} />

      {advanceError && (
        <div data-testid="advance-refused" className="border-warn text-warn border p-3 text-sm">
          {advanceError}
        </div>
      )}

      <button
        type="button"
        onClick={onAdvance}
        disabled={nextBlocked}
        className="border-ink border px-4 py-2 text-sm disabled:opacity-50"
      >
        Next step
      </button>
    </section>
  );
}
