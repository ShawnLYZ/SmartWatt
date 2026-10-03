import type { Telemetry } from "../types/contract";

const LABELS: Record<string, string> = {
  simulator: "SIMULATED",
  replay: "REPLAY",
};

/**
 * Says plainly where the numbers come from. A simulated run presented as a
 * live one would be the exact dishonesty non-negotiable #2 rules out.
 */
export function SourceBadge({ source }: { source: Telemetry["source"] }) {
  // LIVE is gated on an explicit device check rather than a lookup, so no
  // unrecognised source can ever spell its way into claiming live data.
  const label = source === "device" ? "LIVE" : (LABELS[source] ?? source);
  return (
    <span
      data-testid="source-badge"
      className={`figure border px-2 py-0.5 text-[10px] tracking-widest ${
        source === "device"
          ? "border-ink text-ink"
          : "border-warn text-warn"
      }`}
    >
      {label}
    </span>
  );
}
