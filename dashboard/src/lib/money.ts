/**
 * Money arrives from the API as a STRING and is formatted as a string.
 * `Number()` never touches it: the server computes in Decimal precisely so
 * the browser cannot reintroduce binary floating point on the way in.
 */
export function formatRinggit(value: string): string {
  if (typeof value !== "string") {
    throw new TypeError(
      `money must be a string, got ${typeof value}. The API returns money ` +
        `as strings; coercing to number defeats the Decimal discipline.`,
    );
  }

  const negative = value.trimStart().startsWith("-");
  const digits = negative ? value.trim().slice(1) : value.trim();
  const [whole = "0", fraction = ""] = digits.split(".");

  // Round half up at two places, on the string, without going via a float.
  let cents = BigInt(whole) * 100n + BigInt((fraction + "00").slice(0, 2));
  if ((fraction[2] ?? "0") >= "5") cents += 1n;

  const grouped = (cents / 100n)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const remainder = (cents % 100n).toString().padStart(2, "0");

  // A negative amount that rounds to exactly zero cents IS zero: money has
  // exact decimal semantics, and a signed zero ringgit figure is
  // meaningless on a bill. The sign is captured from the input above but
  // only rendered once the rounded magnitude is known to be nonzero.
  const sign = negative && cents !== 0n ? "-" : "";
  return `${sign}RM ${grouped}.${remainder}`;
}

/**
 * Multiplies a money-string rate by a physical quantity (kWh, hours)
 * without ever widening the money side to a float. Plain `rate * qty`
 * would first coerce `rate` through `Number()`, which is exactly the
 * Decimal-defeating move `formatRinggit` exists to refuse -- this is the
 * same refusal, extended to the one arithmetic op the dashboard actually
 * performs on money (rate x quantity for a per-hour or per-appliance
 * figure).
 *
 * The rate is parsed digit-by-digit into a BigInt scaled by its own
 * number of decimal places, the same technique `formatRinggit` uses. The
 * quantity legitimately IS a float -- it is a measurement, not currency --
 * so it is quantised to a fixed 6 decimal places via `toFixed` (i.e.
 * through a decimal STRING, not a second float multiplication) before it
 * joins the same integer arithmetic. From there to the return, nothing is
 * a float: the product is rounded half-up to `dp` places entirely in
 * BigInt and handed back as a plain decimal string, ready for
 * `formatRinggit`.
 */
export function multiplyRinggit(rate: string, quantity: number, dp = 4): string {
  if (typeof rate !== "string") {
    throw new TypeError(
      `money must be a string, got ${typeof rate}. The API returns money ` +
        `as strings; coercing to number defeats the Decimal discipline.`,
    );
  }

  const rateNegative = rate.trim().startsWith("-");
  const rateDigits = rateNegative ? rate.trim().slice(1) : rate.trim();
  const [rateWhole = "0", rateFraction = ""] = rateDigits.split(".");
  const rateScale = rateFraction.length;
  const rateScaled = BigInt(rateWhole + rateFraction);

  // The one float operation in this function: quantise the measurement to
  // a fixed decimal STRING before it enters the integer arithmetic. 6dp
  // is ample headroom for kWh/hours/watts and comfortably exceeds the
  // `dp` this function is ever asked to round to.
  const QUANTITY_DP = 6;
  const quantityNegative = quantity < 0;
  const [qWhole = "0", qFraction = ""] = Math.abs(quantity)
    .toFixed(QUANTITY_DP)
    .split(".");
  const quantityScaled = BigInt(qWhole + qFraction);

  // Both operands are magnitudes (sign stripped above), so this product
  // is always >= 0, scaled by 10^(rateScale + QUANTITY_DP).
  let product = rateScaled * quantityScaled;

  // Round down to `dp` places, half up, without ever leaving BigInt.
  const shift = rateScale + QUANTITY_DP - dp;
  if (shift > 0) {
    const divisor = 10n ** BigInt(shift);
    const remainder = product % divisor;
    product /= divisor;
    if (remainder * 2n >= divisor) product += 1n;
  } else if (shift < 0) {
    product *= 10n ** BigInt(-shift);
  }

  const scale = 10n ** BigInt(dp);
  const whole = (product / scale).toString();
  const fraction = (product % scale).toString().padStart(dp, "0");
  // Opposite signs multiply to a negative; matching signs (including both
  // positive) multiply to a positive. A product that rounds to exactly
  // zero has no sign worth keeping -- the same rule formatRinggit applies
  // to a negative amount that rounds to zero cents.
  const sign = rateNegative !== quantityNegative && product !== 0n ? "-" : "";
  return `${sign}${whole}.${fraction}`;
}

/**
 * Physical quantities are floats. They are measurements, not currency, and
 * carry no accent colour.
 */
export function formatUnit(value: number, unit: string, dp = 1): string {
  return `${value.toFixed(dp)} ${unit}`;
}
