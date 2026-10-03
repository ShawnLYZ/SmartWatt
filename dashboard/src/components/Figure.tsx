import { UNAVAILABLE_MARK } from "../lib/constants";
import { formatRinggit, formatUnit } from "../lib/money";

interface FigureProps {
  label: string;
  /**
   * `null` means the input this figure is derived from is UNAVAILABLE --
   * `/api/month` has not returned, or returned an error. It renders as an
   * explicit unavailable mark, never as `RM 0.00` or `0 g`: substituting a
   * zero for a missing input puts an invented figure on screen and states
   * it as fact (non-negotiable #2).
   */
  value: string | number | null;
  unit?: string;
  dp?: number;
  /** Money gets the accent colour. Nothing else does. */
  money?: boolean;
  stale?: boolean;
  /** Hover explanation for the unavailable state. Ignored when a value is present. */
  unavailableTitle?: string;
}

export function Figure({
  label,
  value,
  unit = "",
  dp = 1,
  money = false,
  stale = false,
  unavailableTitle = "Unavailable — this figure has no source data right now",
}: FigureProps) {
  const unavailable = value === null;
  const text = unavailable
    ? UNAVAILABLE_MARK
    : money
      ? formatRinggit(value as string)
      : formatUnit(value as number, unit, dp);

  return (
    <div
      data-testid={`figure-${label.toLowerCase().replace(/\s+/g, "-")}`}
      data-stale={stale ? "true" : "false"}
      data-unavailable={unavailable ? "true" : "false"}
      className={stale ? "opacity-40" : undefined}
    >
      <div className="text-ink-muted text-xs uppercase tracking-wide">
        {label}
      </div>
      <div
        // An unavailable figure is NOT money, whatever the `money` flag
        // says about the field it stands in for -- so it never takes the
        // ringgit accent. The accent means "this is an amount"; there is
        // no amount here.
        className={`figure text-2xl ${
          unavailable ? "text-ink-muted" : money ? "text-ringgit" : "text-ink"
        }`}
        title={unavailable ? unavailableTitle : undefined}
      >
        {text}
      </div>
    </div>
  );
}
