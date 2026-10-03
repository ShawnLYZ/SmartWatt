/** Reserved ledger id for power the system cannot attribute. */
export const RESIDUAL_KEY = "__residual__";

/** US8: the unattributed band is NAMED, not hidden. */
export const RESIDUAL_LABEL = "Unidentified";

/** Prefix the tracker assigns to a detected-but-unnamed load. */
export const UNKNOWN_PREFIX = "unknown_";

/**
 * Rendered wherever a figure's input is genuinely unavailable.
 *
 * Never a zero, and never the last known value. A missing input rendered
 * as `RM 0.00` or `0 g` is an invented figure asserted as fact, which is
 * precisely the concealment non-negotiable #2 exists to forbid -- and it
 * is worse than a blank, because a zero is a number a reader will act on.
 */
export const UNAVAILABLE_MARK = "—";
