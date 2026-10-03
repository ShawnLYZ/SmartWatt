import { render, screen } from "@testing-library/react";
import { cloneElement, type ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { Live } from "../src/screens/Live/Live";
import { RESIDUAL_INK, RESIDUAL_LABEL } from "../src/screens/Live/StackedPower";
import { loadExample } from "./fixtures";
import type { Telemetry } from "../src/types/contract";

// jsdom never runs layout: there is no ResizeObserver, and every element's
// getBoundingClientRect() reports zeroes. Recharts' real ResponsiveContainer
// measures itself with both, so under jsdom it either throws (no
// ResizeObserver) or settles on 0x0 and logs a width/height warning.
// Cloning its single child with a fixed pixel size -- the same shape the
// real component injects once it HAS measured -- keeps Area/AreaChart/
// Tooltip genuinely rendering, so this is still real chart output, just
// deterministically sized instead of measured from a DOM that has none.
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({
      children,
    }: {
      // Typed to the width/height recharts' own chart components accept
      // (the same two props the real ResponsiveContainer injects, per its
      // source) rather than left as bare `ReactElement` -- React 19 types
      // default that to `<unknown>` props, and `cloneElement` would then
      // reject `width`/`height` as unknown properties.
      children: ReactElement<{ width?: number; height?: number }>;
    }) => cloneElement(children, { width: 800, height: 400 }),
  };
});

const SERIES = {
  t: [1754035200, 1754035201, 1754035202],
  total_p: [902.5, 903.1, 901.8],
  // Ruling D: `series` below is indexed against `t_min` (the one-minute
  // ledger rollup axis), never against `t` (the 1 Hz telemetry axis) --
  // three consecutive minute timestamps, matching the length of every
  // array under `series`.
  t_min: [1754035200, 1754035260, 1754035320],
  series: { kettle: [848, 848, 848], __residual__: [54.5, 55.1, 53.8] },
};

function renderLive(
  telemetry: Telemetry | null,
  overrides: Partial<Parameters<typeof Live>[0]> = {},
) {
  return render(
    <Live
      telemetry={telemetry}
      stale={false}
      series={SERIES}
      marginalRinggitPerHour="0.27"
      gramsCo2PerHour={179.6}
      {...overrides}
    />,
  );
}

/** Every band recharts actually painted, in DOM order. */
const areaCurves = (container: HTMLElement) =>
  Array.from(container.querySelectorAll(".recharts-area-curve"));

describe("Live", () => {
  it("renders total real power", () => {
    renderLive(loadExample("telemetry-single-load"));
    expect(screen.getByTestId("figure-power")).toHaveTextContent("903 W");
  });

  it("renders the burn rate from the MARGINAL cost, not the nominal rate", () => {
    renderLive(loadExample("telemetry-single-load"), {
      marginalRinggitPerHour: "10.295",
    });
    expect(screen.getByTestId("figure-cost-per-hour")).toHaveTextContent(
      "RM 10.30",
    );
  });

  it("renders grams of CO2 per hour beside the cost", () => {
    renderLive(loadExample("telemetry-single-load"));
    expect(screen.getByTestId("figure-co₂-per-hour")).toHaveTextContent("180 g");
  });

  it("renders the cost per hour unavailable, never RM 0.00, when /api/month has not returned", () => {
    // The staleness clock tracks the SOCKET, so with a healthy socket and
    // a failing /api/month a substituted "0.00" rendered fresh and
    // un-staled: "Cost per hour RM 0.00" stated as current fact.
    renderLive(loadExample("telemetry-single-load"), {
      marginalRinggitPerHour: null,
    });
    const figure = screen.getByTestId("figure-cost-per-hour");
    expect(figure).toHaveAttribute("data-unavailable", "true");
    expect(figure).not.toHaveTextContent("RM");
    expect(figure).toHaveTextContent("—");
  });

  it("renders the CO2 per hour unavailable, never 0 g, when no carbon factor has loaded", () => {
    renderLive(loadExample("telemetry-single-load"), {
      gramsCo2PerHour: null,
    });
    const figure = screen.getByTestId("figure-co₂-per-hour");
    expect(figure).toHaveAttribute("data-unavailable", "true");
    expect(figure).not.toHaveTextContent("0 g");
  });

  it("renders measured voltage and frequency", () => {
    renderLive(loadExample("telemetry-single-load"));
    expect(screen.getByTestId("figure-voltage")).toHaveTextContent("239.4 V");
    expect(screen.getByTestId("figure-frequency")).toHaveTextContent(
      "49.98 Hz",
    );
  });

  it("renders the detection floor", () => {
    renderLive(loadExample("telemetry-single-load"));
    expect(screen.getByTestId("figure-detection-floor")).toHaveTextContent(
      "8.5 W",
    );
  });

  it("renders the source badge", () => {
    renderLive(loadExample("telemetry-simulator-null-health"));
    expect(screen.getByTestId("source-badge")).toHaveTextContent("SIMULATED");
  });

  it("labels the residual band Unidentified in the sensor strip", () => {
    // Scoped deliberately: this reads Live's OWN figure label, not the
    // chart. The chart's residual band has its own coverage below, because
    // `screen.getByText(RESIDUAL_LABEL)` resolves to this label and to
    // this label only -- it passed with the chart's <Area> deleted
    // outright.
    renderLive(loadExample("telemetry-single-load"));
    expect(screen.getByTestId("residual-current")).toHaveTextContent(
      RESIDUAL_LABEL,
    );
  });

  it("draws one chart band per appliance plus the residual band", () => {
    const { container } = renderLive(loadExample("telemetry-single-load"));
    // SERIES carries one appliance (kettle) plus __residual__.
    const appliances = Object.keys(SERIES.series).filter(
      (key) => key !== "__residual__",
    );
    expect(areaCurves(container)).toHaveLength(appliances.length + 1);
    expect(
      container.querySelectorAll(
        `.recharts-area-curve[stroke="${RESIDUAL_INK}"]`,
      ),
    ).toHaveLength(1);
  });

  it("draws the residual band in the chart even when it is zero", () => {
    // US8: the unattributed band is NAMED, not hidden -- including at
    // zero, where dropping it is invisible in every other assertion.
    //
    // Two minute-samples, not one: recharts draws a single-point series
    // as a `.recharts-area-dot` with no path at all, so a one-point
    // fixture would make this assertion fail for a reason that has
    // nothing to do with the residual band. A real 15-minute window
    // carries ~15 points anyway.
    const { container } = renderLive(
      loadExample("telemetry-simulator-null-health"),
      {
        series: {
          t: [1, 2],
          total_p: [0, 0],
          t_min: [1, 2],
          series: { __residual__: [0, 0] },
        },
      },
    );
    expect(areaCurves(container)).toHaveLength(1);
    expect(
      container.querySelectorAll(
        `.recharts-area-curve[stroke="${RESIDUAL_INK}"]`,
      ),
    ).toHaveLength(1);
  });

  it("takes the live residual figure from 1 Hz telemetry, not the 30 s series poll", () => {
    // The fixture's attribution.residual_w is -1.3; the inline series
    // carries a deliberately DIFFERENT -9.9 as its last per-minute value.
    // Reading the series first put a figure up to 60-90 s old beside live
    // Vrms and frequency under a staleness clock that only watches the
    // socket. Identical values in both sources cannot tell the two
    // orderings apart, which is why they differ here.
    renderLive(loadExample("telemetry-negative-residual"), {
      series: {
        t: [1, 2],
        total_p: [44.9, 44.9],
        t_min: [1, 2],
        series: { desk_fan: [46.2, 46.2], __residual__: [-9.9, -9.9] },
      },
    });
    expect(screen.getByTestId("residual-current")).toHaveTextContent("-1.3 W");
    expect(screen.getByTestId("residual-current")).not.toHaveTextContent(
      "-9.9 W",
    );
  });

  it("stacks the negative residual below the zero axis in the chart itself", () => {
    // Ruling V: the other residual assertions resolve through Live's
    // `residualNow` figure, never through StackedPower's rendered chart.
    // This one reads the chart's own Y axis. Spec: "Where the residual is
    // negative the band renders below the axis rather than being
    // dropped." With stackOffset="sign" and a domain floor of
    // Math.min(0, dataMin), a negative __residual__ value pulls the axis
    // domain below zero, so at least one rendered tick must be negative.
    // Pinned at the pre-fix default domain (`[0, 'auto']`), every tick is
    // >= 0 and this fails -- that gap is exactly what this test exists to
    // catch, so it must fail against the current, unfixed component.
    const { container } = renderLive(loadExample("telemetry-negative-residual"), {
      series: {
        t: [1, 2],
        total_p: [44.9, 44.9],
        t_min: [1, 2],
        series: { desk_fan: [46.2, 46.2], __residual__: [-1.3, -1.3] },
      },
    });
    const tickLabels = Array.from(
      container.querySelectorAll(
        ".recharts-yAxis .recharts-cartesian-axis-tick-value",
      ),
    ).map((el) => el.textContent ?? "");
    // Sanity gate: fail loudly and distinctly if the axis rendered no
    // ticks at all (a broken selector), rather than silently agreeing
    // with "no negative tick found" for the wrong reason.
    expect(tickLabels.length).toBeGreaterThan(0);
    expect(tickLabels.some((label) => /^-\d/.test(label))).toBe(true);
  });

  it("says so when the 15-minute series is unavailable, rather than redrawing an old window", () => {
    // getSeries is a 30 s REST poll. Swallowing its failure left the
    // previous window on screen indefinitely, labelled "Last 15 minutes"
    // and indistinguishable from a current one.
    const { container } = renderLive(loadExample("telemetry-single-load"), {
      series: null,
    });
    expect(screen.getByTestId("series-unavailable")).toHaveTextContent(
      /not available/i,
    );
    expect(areaCurves(container)).toHaveLength(0);
    // The 1 Hz figures are a different source and stay real.
    expect(screen.getByTestId("figure-power")).toHaveTextContent("903 W");
  });

  it("stales every figure when the socket goes quiet", () => {
    renderLive(loadExample("telemetry-single-load"), { stale: true });
    expect(screen.getByTestId("figure-power")).toHaveAttribute(
      "data-stale",
      "true",
    );
  });

  it("renders an empty state before the first payload", () => {
    renderLive(null);
    expect(screen.getByText(/waiting for telemetry/i)).toBeInTheDocument();
  });

  it("renders no connection indicator of its own -- the header owns it", () => {
    // Spec, Data flow: one hook owns connection state and every screen
    // reads it. It lives in App's header now, so a second copy here would
    // only be a duplicate testid for `getByTestId` to throw on.
    const { container } = renderLive(loadExample("telemetry-single-load"));
    expect(container.querySelectorAll('[data-testid="connection"]')).toHaveLength(
      0,
    );
    const empty = renderLive(null);
    expect(
      empty.container.querySelectorAll('[data-testid="connection"]'),
    ).toHaveLength(0);
  });
});
