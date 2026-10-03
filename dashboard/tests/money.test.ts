import { describe, expect, it } from "vitest";
import { formatRinggit, formatUnit, multiplyRinggit } from "../src/lib/money";

describe("money", () => {
  it("formats a string amount", () => {
    expect(formatRinggit("118.295")).toBe("RM 118.30");
  });

  it("formats an already-rounded amount unchanged", () => {
    expect(formatRinggit("10.30")).toBe("RM 10.30");
  });

  it("handles zero", () => {
    expect(formatRinggit("0.00")).toBe("RM 0.00");
  });

  it("handles negative amounts", () => {
    expect(formatRinggit("-3.5")).toBe("-RM 3.50");
  });

  it("suppresses the sign when a negative amount rounds to zero", () => {
    expect(formatRinggit("-0.001")).toBe("RM 0.00");
    expect(formatRinggit("-0.004")).toBe("RM 0.00");
  });

  it("keeps the sign when a negative amount rounds to a nonzero figure", () => {
    // Guards against the fix over-applying: -0.006 rounds up to -0.01,
    // still genuinely negative, so the sign must survive.
    expect(formatRinggit("-0.006")).toBe("-RM 0.01");
  });

  it("rounds half up, not half even", () => {
    expect(formatRinggit("0.125")).toBe("RM 0.13");
  });

  it("does not lose precision on large values", () => {
    expect(formatRinggit("12345.675")).toBe("RM 12,345.68");
  });

  it("throws on a number rather than silently coercing", () => {
    // @ts-expect-error deliberate: money must never arrive as a number
    expect(() => formatRinggit(118.295)).toThrow();
  });

  it("formats physical units as numbers", () => {
    expect(formatUnit(902.5, "W", 0)).toBe("903 W");
    expect(formatUnit(49.98, "Hz", 2)).toBe("49.98 Hz");
  });

  it("formats a negative physical quantity", () => {
    expect(formatUnit(-1.3, "W", 1)).toBe("-1.3 W");
  });
});

// Ruling E: money is never widened to a float, even transiently, when it
// is being multiplied by a physical quantity (rate x kWh, rate x hours).
// These cases are the verified table from the controller ruling; each one
// is checked both as the raw dp=4 string and as what formatRinggit does
// with it, since that composition is how the app actually renders it.
describe("multiplyRinggit", () => {
  it("multiplies a rate by a whole-number quantity", () => {
    expect(multiplyRinggit("0.295", 120)).toBe("35.4000");
    expect(formatRinggit(multiplyRinggit("0.295", 120))).toBe("RM 35.40");
  });

  it("rounds half up at the 4th decimal place", () => {
    // 10.30 * 0.9025 = 9.29575 exactly; the digit at the 5th place is a
    // 5, which rounds the 4th place up from 7 to 8.
    expect(multiplyRinggit("10.30", 0.9025)).toBe("9.2958");
    expect(formatRinggit(multiplyRinggit("10.30", 0.9025))).toBe("RM 9.30");
  });

  it("handles a negative quantity", () => {
    expect(multiplyRinggit("0.295", -0.4)).toBe("-0.1180");
    expect(formatRinggit(multiplyRinggit("0.295", -0.4))).toBe("-RM 0.12");
  });

  it("handles a zero quantity", () => {
    expect(multiplyRinggit("0.295", 0)).toBe("0.0000");
    expect(formatRinggit(multiplyRinggit("0.295", 0))).toBe("RM 0.00");
  });

  it("throws on a non-string rate rather than silently coercing", () => {
    // @ts-expect-error deliberate: the rate must never arrive as a number
    expect(() => multiplyRinggit(0.295, 120)).toThrow(TypeError);
  });

  it("handles a rate with no decimal point at all", () => {
    // rateScale is 0, so the `shift` that rescales the BigInt product is
    // driven entirely by QUANTITY_DP. An off-by-one there is invisible
    // for every rate that happens to carry decimals.
    expect(multiplyRinggit("5", 2)).toBe("10.0000");
    expect(formatRinggit(multiplyRinggit("5", 2))).toBe("RM 10.00");
  });

  it("rounds a rate carrying more decimals than dp", () => {
    // 0.123456 x 1 = 0.123456; rounded half up at the 4th place the 5th
    // digit is a 5, so the 4th goes from 4 to 5.
    expect(multiplyRinggit("0.123456", 1)).toBe("0.1235");
    expect(formatRinggit(multiplyRinggit("0.123456", 1))).toBe("RM 0.12");
  });

  it("does not lose precision on a large quantity", () => {
    // The whole point of the BigInt path: 0.295 x 999999999 in float64 is
    // 294999999.70500004..., and any `.toFixed(4)` on that is a rounded
    // float, not the exact product.
    expect(multiplyRinggit("0.295", 999_999_999)).toBe("294999999.7050");
    expect(formatRinggit(multiplyRinggit("0.295", 999_999_999))).toBe(
      "RM 294,999,999.71",
    );
  });
});
