import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  CliffGauge,
  DEFAULT_BANDS,
  visibleBands,
} from "../src/components/CliffGauge/CliffGauge";
import { loadTariffBands } from "./fixtures";

/**
 * Mirrors CliffGauge's own `axisMax` derivation exactly (kWh axis: the
 * larger of projected/boundary/mtd, with 15% headroom; a 100 kWh floor so
 * an all-zero payload never divides by zero). Duplicated here rather than
 * imported so these tests exercise the real formula's shape against real
 * fixture inputs, not a hand-picked axisMax chosen to make the arithmetic
 * convenient. If the component's formula changes, this must change with
 * it -- that's the point: it keeps the test honest about what geometry
 * the component would actually produce.
 */
function axisMaxFor(
  mtdKwh: number,
  projectedKwh: number,
  boundary: number | null,
) {
  return Math.max(projectedKwh, boundary ?? 0, mtdKwh) * 1.15 || 100;
}

/**
 * A SYNTHETIC component-level input, not a capture of `/api/month`.
 *
 * `band_step` and `marginal` here are chosen to be two clearly distinct,
 * easily-read figures, because what these tests check is that the gauge
 * renders BOTH with distinct labels (US22) and formats them from strings
 * -- not what the tariff engine computes. The server's real figures for a
 * 341 kWh month inside the discount window are `band_step: "7.50"` and
 * `marginal: "0.20"` (see the corrected fixture in month.test.tsx, which
 * DOES claim to be a whole `/api/month` payload and so must match the
 * server band-for-band). Do not read these two values as real output.
 */
const OVER = {
  boundary_kwh: "400",
  kwh_remaining: "-17.000",
  current_rate_sen: "27.0",
  next_rate_sen: "29.5",
  band_step: "10.00",
  marginal: "10.30",
  culprit: {
    appliance_id: "incandescent_lamp",
    projected_kwh: "116.500",
    sufficient_alone: true,
  },
};

const UNDER = {
  ...OVER,
  kwh_remaining: "59.000",
  culprit: null,
};

describe("CliffGauge", () => {
  it("renders the boundary", () => {
    render(<CliffGauge mtdKwh={341} projectedKwh={417} cliff={OVER} />);
    expect(screen.getByTestId("gauge-boundary")).toHaveTextContent("400");
  });

  it("renders month-to-date and projected markers", () => {
    render(<CliffGauge mtdKwh={341} projectedKwh={417} cliff={OVER} />);
    expect(screen.getByTestId("marker-mtd")).toHaveTextContent("341");
    expect(screen.getByTestId("marker-projected")).toHaveTextContent("417");
  });

  it("renders the rate as a visible step function", () => {
    render(<CliffGauge mtdKwh={341} projectedKwh={417} cliff={OVER} />);
    const steps = screen.getAllByTestId(/^rate-step-/);
    expect(steps.length).toBeGreaterThan(3);
  });

  it("renders BOTH penalty numbers with distinct labels", () => {
    render(<CliffGauge mtdKwh={341} projectedKwh={417} cliff={OVER} />);
    expect(screen.getByTestId("band-step")).toHaveTextContent("RM 10.00");
    expect(screen.getByTestId("marginal")).toHaveTextContent("RM 10.30");
    expect(screen.getByText(/re-prices your whole month/i)).toBeInTheDocument();
    expect(screen.getByText(/the next unit costs/i)).toBeInTheDocument();
  });

  it("names the culprit", () => {
    render(<CliffGauge mtdKwh={341} projectedKwh={417} cliff={OVER} />);
    expect(screen.getByTestId("culprit")).toHaveTextContent(
      "incandescent_lamp",
    );
  });

  it("says so when no single appliance would get you under", () => {
    render(
      <CliffGauge
        mtdKwh={341}
        projectedKwh={417}
        cliff={{
          ...OVER,
          culprit: { ...OVER.culprit, sufficient_alone: false },
        }}
      />,
    );
    expect(screen.getByText(/not enough on its own/i)).toBeInTheDocument();
  });

  it("renders a neutral state when the projection is under the boundary", () => {
    render(<CliffGauge mtdKwh={200} projectedKwh={341} cliff={UNDER} />);
    expect(screen.getByTestId("cliff-gauge")).toHaveAttribute(
      "data-state",
      "clear",
    );
  });

  it("does not manufacture alarm when nothing is wrong", () => {
    render(<CliffGauge mtdKwh={200} projectedKwh={341} cliff={UNDER} />);
    expect(screen.queryByTestId("culprit")).not.toBeInTheDocument();
    expect(screen.getByTestId("kwh-remaining")).toHaveTextContent("59");
  });

  it("flags the over state", () => {
    render(<CliffGauge mtdKwh={341} projectedKwh={417} cliff={OVER} />);
    expect(screen.getByTestId("cliff-gauge")).toHaveAttribute(
      "data-state",
      "over",
    );
  });

  it("renders without a boundary in the open-ended band", () => {
    render(
      <CliffGauge
        mtdKwh={1800}
        projectedKwh={2100}
        cliff={{
          boundary_kwh: null,
          kwh_remaining: null,
          current_rate_sen: null,
          next_rate_sen: null,
          band_step: "0.00",
          marginal: "0.32",
          culprit: null,
        }}
      />,
    );
    expect(screen.getByTestId("cliff-gauge")).toHaveAttribute(
      "data-state",
      "no-boundary",
    );
  });

  it("renders money from strings, never coerced", () => {
    // "10.00" cannot tell formatRinggit's string/BigInt path apart from a
    // naive `RM ${Number(value).toFixed(2)}` coercion -- both produce the
    // same text. "1.005" can: its nearest float64 sits just below the
    // true halfway point, so `(1.005).toFixed(2)` rounds DOWN to "1.00",
    // while formatRinggit rounds half-up on the decimal string itself and
    // correctly gives "1.01". Don't "simplify" this back to "10.00".
    render(
      <CliffGauge
        mtdKwh={341}
        projectedKwh={417}
        cliff={{ ...OVER, band_step: "1.005" }}
      />,
    );
    expect(screen.getByTestId("band-step")).toHaveTextContent("RM 1.01");
  });
});

/**
 * Ruling W's fix, tested directly against the pure function rather than
 * through the DOM. `screen.getAllByTestId(/^rate-step-/).length > 3`
 * (above) cannot distinguish this filter from the brief's original,
 * broken one -- both render 6 steps for the OVER fixture. Only the exact
 * from/to/sen table catches the difference: the broken filter draws the
 * 400-480 kWh span at the open-ended band's 31.5 sen/kWh instead of the
 * real 500-band's 29.5.
 */
describe("visibleBands (Ruling W)", () => {
  it("clamps the OVER fixture's staircase to the true Tariff D rate at the axis edge", () => {
    const axisMax = axisMaxFor(341, 417, Number(OVER.boundary_kwh));
    const visible = visibleBands(DEFAULT_BANDS, axisMax);

    expect(visible).toHaveLength(6);
    expect(visible.slice(0, 5)).toEqual([
      { up_to: 100, sen: 18.0, from: 0, to: 100 },
      { up_to: 150, sen: 18.0, from: 100, to: 150 },
      { up_to: 200, sen: 22.0, from: 150, to: 200 },
      { up_to: 300, sen: 25.0, from: 200, to: 300 },
      { up_to: 400, sen: 27.0, from: 300, to: 400 },
    ]);

    // The band that actually governs 400 kWh up to the axis edge: 500's
    // LOWER edge (400) is inside the visible range, so it draws the real
    // 29.5 sen/kWh here -- not the open-ended band's 31.5, which the
    // brief's original `up_to <= axisMax` filter would have drawn across
    // this exact span (Ruling W).
    const last = visible[5];
    expect(last).toBeDefined();
    expect(last).toMatchObject({ up_to: 500, sen: 29.5, from: 400 });
    expect(last!.to).toBeCloseTo(axisMax, 5);

    // And the open-ended band must be absent -- this is the assertion
    // the broken filter fails: it admits `up_to: null` unconditionally,
    // because `null ?? axisMax <= axisMax` is always true.
    expect(visible.some((b) => b.up_to === null)).toBe(false);
  });

  it("keeps the full staircase, including the open-ended band, when the axis runs past 1300 kWh", () => {
    const axisMax = axisMaxFor(1800, 2100, null);
    const visible = visibleBands(DEFAULT_BANDS, axisMax);

    expect(visible).toHaveLength(10);
    expect(visible.slice(0, 9)).toEqual([
      { up_to: 100, sen: 18.0, from: 0, to: 100 },
      { up_to: 150, sen: 18.0, from: 100, to: 150 },
      { up_to: 200, sen: 22.0, from: 150, to: 200 },
      { up_to: 300, sen: 25.0, from: 200, to: 300 },
      { up_to: 400, sen: 27.0, from: 300, to: 400 },
      { up_to: 500, sen: 29.5, from: 400, to: 500 },
      { up_to: 700, sen: 30.0, from: 500, to: 700 },
      { up_to: 800, sen: 30.5, from: 700, to: 800 },
      { up_to: 1300, sen: 31.0, from: 800, to: 1300 },
    ]);

    const last = visible[9];
    expect(last).toBeDefined();
    expect(last).toMatchObject({ up_to: null, sen: 31.5, from: 1300 });
    expect(last!.to).toBeCloseTo(axisMax, 5);
  });
});

/**
 * Ruling W arriving from the other direction.
 *
 * `DEFAULT_BANDS` is a hand-copy of `tariff/smartwatt_tariff/config/
 * tariff.toml`. It is correct today, but that file opens with "Re-verify
 * every figure against a current bill before any public demonstration" --
 * it is EXPECTED to change. When it does, nothing in the dashboard
 * notices, and the gauge quietly draws a staircase that is no longer the
 * published tariff: a real Tariff D band rendered at the wrong sen/kWh,
 * which is precisely the misrepresentation the gauge exists to prevent.
 *
 * So the copy is bound to its source here. A schedule change now fails
 * this test, naming both sides, instead of silently drawing a false
 * staircase at the demonstration.
 */
describe("DEFAULT_BANDS is bound to the published tariff schedule", () => {
  /**
   * "18.0" and "18.00" are the same published rate; compare on a
   * canonical decimal string so a harmless formatting change in the TOML
   * is not a false alarm. Deliberately string-only -- no Number() goes
   * anywhere near a sen/kWh figure here.
   */
  const canonical = (decimal: string): string => {
    const [whole = "0", fraction = ""] = decimal.trim().split(".");
    const trimmed = fraction.replace(/0+$/, "");
    return trimmed ? `${whole}.${trimmed}` : whole;
  };

  it("matches tariff.toml band for band", () => {
    const published = loadTariffBands();

    // Sanity gate: a regex that silently matched nothing would make this
    // whole test agree with itself for the wrong reason.
    expect(published.length).toBeGreaterThan(0);
    expect(published.at(-1)?.up_to_kwh).toBeNull();

    expect(
      DEFAULT_BANDS.map((band) => ({
        up_to_kwh: band.up_to === null ? null : String(band.up_to),
        // `toFixed` formats a number that is ALREADY a number in the
        // source; it does not coerce anything out of a string. 4 places
        // is ample for a sen/kWh figure and is trimmed back by
        // `canonical` immediately.
        sen_per_kwh: canonical(band.sen.toFixed(4)),
      })),
    ).toEqual(
      published.map((band) => ({
        up_to_kwh: band.up_to_kwh === null ? null : canonical(band.up_to_kwh),
        sen_per_kwh: canonical(band.sen_per_kwh),
      })),
    );
  });
});
