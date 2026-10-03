import type { ActionRow, ControlResult, PendingCut, RulesConfig } from "../../lib/api";

/**
 * `/api/appliances` (`ApplianceRow`, lib/api.ts) is shaped `id`/`0|1`;
 * this is deliberately a DIFFERENT shape -- `appliance_id`/boolean flags
 * -- because that is what the JSX below reads naturally as
 * `device.protected && (...)`. App.tsx does the conversion (Ruling D);
 * this file never imports `ApplianceRow`, so there is nothing here that
 * could accidentally render a bare `1`/`0` where a boolean was meant.
 */
interface Device {
  appliance_id: string;
  display_name: string;
  plug_device: string | null;
  protected: boolean;
  heating: boolean;
}

interface ControlProps {
  /**
   * Ruling B: ONLY devices with a registered plug -- filtered in
   * App.tsx, not here. A plugless device's toggle could only ever
   * produce REFUSED_UNREGISTERED, which demonstrates missing hardware,
   * not the safety gate. laptop_charger (protected, but WITH a plug)
   * is not filtered out by this rule: its toggle staying reachable is
   * US49's whole demonstration.
   */
  devices: Device[];
  pending: PendingCut[];
  actions: ActionRow[];
  onCommand: (applianceId: string, command: "ON" | "OFF") => void;
  onCancel: (pendingId: string) => void;
  lastOutcome: ControlResult | null;
  /**
   * The ACTIVE rule thresholds from `/api/rules`, `null` when that fetch
   * has not succeeded (never yet, or a failed retry -- see App.tsx).
   * There is deliberately no fallback rendering here: a thresholds panel
   * that silently guessed `is_demo` on a failed fetch would be exactly
   * the "small dishonesty" this panel exists to rule out, so it renders
   * nothing at all rather than a guess.
   */
  rules: RulesConfig | null;
}

export function Control({
  devices, pending, actions, onCommand, onCancel, lastOutcome, rules,
}: ControlProps) {
  return (
    <section className="space-y-8">
      {rules && (
        <div
          data-testid="rules-panel"
          data-demo={rules.is_demo ? "true" : "false"}
          className={
            rules.is_demo
              ? "border-warn border-2 p-4"
              : "border-rule border p-4"
          }
        >
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Active thresholds</h2>
            {rules.is_demo ? (
              // Deliberately loud: bg/text INVERTED (warn fill, paper
              // text), not merely a coloured label -- someone glancing
              // at a projected screen must not mistake a 30 s demo
              // threshold for the shipped 4-hour default.
              <span
                data-testid="demo-badge"
                className="bg-warn text-paper px-2 py-1 text-xs font-bold uppercase tracking-wide"
              >
                Demo values — not the shipped defaults
              </span>
            ) : (
              <span
                data-testid="shipped-badge"
                className="text-ink-muted text-[10px] uppercase tracking-wide"
              >
                shipped defaults
              </span>
            )}
          </div>
          <dl className="grid grid-cols-3 gap-4 pt-3 text-xs">
            <div>
              <dt className="text-ink-muted uppercase tracking-wide">
                Left-on warn
              </dt>
              {/* `figure` on the value only, not the `<dt>` label above --
                  that class is for digits that must not jitter as they
                  change (index.css), and a label never changes. */}
              <dd className="figure">
                {rules.left_on_seconds}s · {rules.left_on_targets.join(", ")}
              </dd>
            </div>
            <div>
              <dt className="text-ink-muted uppercase tracking-wide">
                Standby band
              </dt>
              <dd className="figure">
                {rules.standby_band_w[0]}–{rules.standby_band_w[1]} W for{" "}
                {rules.standby_seconds}s
              </dd>
            </div>
            <div>
              <dt className="text-ink-muted uppercase tracking-wide">
                Grace period
              </dt>
              <dd className="figure">{rules.grace_seconds}s</dd>
            </div>
          </dl>
        </div>
      )}

      {pending.length > 0 && (
        <div className="border-warn border p-4">
          <h2 className="text-sm font-semibold">Pending cut</h2>
          {pending.map((entry) => (
            <div
              key={entry.id}
              data-testid={`pending-${entry.id}`}
              className="flex items-center justify-between pt-2"
            >
              <span className="text-sm">
                <strong>{entry.appliance_id}</strong> will be switched off by{" "}
                <em>{entry.rule}</em> in{" "}
                <span className="figure">{Math.ceil(entry.remaining_s)}</span> s
              </span>
              <button
                type="button"
                onClick={() => onCancel(entry.id)}
                className="border-ink border px-3 py-1 text-xs"
              >
                Cancel
              </button>
            </div>
          ))}
        </div>
      )}

      <div>
        <h2 className="rule-b pb-2 text-sm font-semibold">Registered plugs</h2>
        {devices.map((device) => (
          <div
            key={device.appliance_id}
            data-testid={`device-${device.appliance_id}`}
            className="rule-b flex items-center justify-between py-3"
          >
            <span className="text-sm">
              {device.display_name}
              {device.protected && (
                <span
                  data-testid="protected-mark"
                  className="text-warn ml-2 text-[10px] uppercase tracking-wide"
                >
                  protected · un-cuttable
                </span>
              )}
              {device.heating && (
                <span
                  data-testid="heating-mark"
                  className="text-ink-muted ml-2 text-[10px] uppercase tracking-wide"
                >
                  heating · one-way
                </span>
              )}
            </span>
            <span className="flex gap-2">
              {/*
                The protected toggle stays ACTIVE, not disabled. US49 asks an
                evaluator to attempt a protected cut and watch the system
                refuse - a greyed-out button proves nothing, and a system
                that hides the attempt cannot demonstrate the guarantee.
              */}
              <button
                type="button"
                onClick={() => onCommand(device.appliance_id, "ON")}
                className="border-ink border px-3 py-1 text-xs"
              >
                On
              </button>
              <button
                type="button"
                onClick={() => onCommand(device.appliance_id, "OFF")}
                className="border-ink border px-3 py-1 text-xs"
              >
                Off
              </button>
            </span>
          </div>
        ))}
      </div>

      {lastOutcome && (
        <div
          data-testid="last-outcome"
          className={
            lastOutcome.outcome.startsWith("REFUSED")
              ? "border-warn text-warn border p-3 text-sm"
              : "border-rule border p-3 text-sm"
          }
        >
          <strong>{lastOutcome.outcome}</strong>
          {lastOutcome.reason && <> — {lastOutcome.reason}</>}
        </div>
      )}

      <div>
        <h2 className="rule-b pb-2 text-sm font-semibold">Action log</h2>
        <table className="w-full">
          {/* Labelled, because the log is read by someone who did not
              write it. Every column below is an unadorned server value:
              "Appliance" is `actions.device`, which carries the APPLIANCE
              id on every row -- rule-sent, user-sent and refused alike
              (gate.py's _log). Before that was made true it held the plug
              name on sent rows and the appliance id on refusals, and an
              unlabelled column made the mixture invisible. */}
          <thead>
            <tr className="rule-b text-ink-muted text-left text-[10px] uppercase tracking-wide">
              <th scope="col" className="py-2 font-normal">Actor</th>
              <th scope="col" className="py-2 font-normal">Appliance</th>
              <th scope="col" className="py-2 font-normal">Command</th>
              <th scope="col" className="py-2 font-normal">Rule</th>
              <th scope="col" className="py-2 font-normal">Outcome</th>
              <th scope="col" className="py-2 font-normal">Reason</th>
            </tr>
          </thead>
          <tbody>
            {actions.map((action, index) => (
              <tr
                key={`${action.ts}-${index}`}
                data-testid={`action-${index}`}
                data-refused={action.outcome.startsWith("REFUSED") ? "true" : "false"}
                className={`rule-b ${
                  action.outcome.startsWith("REFUSED") ? "text-warn" : ""
                }`}
              >
                <td className="figure py-2 text-xs">{action.actor}</td>
                <td className="py-2 text-xs">{action.device}</td>
                <td className="figure py-2 text-xs">{action.command}</td>
                <td className="py-2 text-xs">{action.rule ?? "—"}</td>
                <td className="figure py-2 text-xs">{action.outcome}</td>
                <td className="py-2 text-xs">{action.reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
