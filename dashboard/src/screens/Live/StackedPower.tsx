import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatClock } from "../../lib/time";

export { RESIDUAL_KEY, RESIDUAL_LABEL } from "../../lib/constants";
import { RESIDUAL_KEY, RESIDUAL_LABEL } from "../../lib/constants";

/**
 * A quiet greyscale ramp for appliances; the residual is hatched-grey and
 * always last so it reads as "not explained" rather than as another load.
 */
const APPLIANCE_INK = [
  "#16150f", "#3d3b31", "#5c5a4f", "#7b786c", "#9a978a",
];
/**
 * Exported so the test suite can pick the residual band out of the
 * rendered chart by the stroke recharts actually paints, rather than
 * hand-copying the hex into an assertion that would then agree with
 * itself forever if this changed.
 */
export const RESIDUAL_INK = "#c4c1b6";

export interface StackedPowerProps {
  /**
   * Ruling D: `/api/series` carries two independent axes. `t`/`total_p`
   * are the 1 Hz telemetry samples (~900 points over 15 minutes); `t_min`
   * is the one-minute ledger rollup (~15 points), and `series` below is
   * indexed against THAT axis, never against `t`. `Live` passes
   * `series.t_min` here. The prop keeps the generic name `t` because this
   * component only needs "the x-axis these series line up with" - it
   * doesn't know or care which upstream axis that is.
   */
  t: number[];
  series: Record<string, number[]>;
}

export function StackedPower({ t, series }: StackedPowerProps) {
  const appliances = Object.keys(series)
    .filter((key) => key !== RESIDUAL_KEY)
    .sort();

  const rows = t.map((ts, index) => {
    const row: Record<string, number | string> = { ts };
    for (const key of appliances) row[key] = series[key]?.[index] ?? 0;
    row[RESIDUAL_KEY] = series[RESIDUAL_KEY]?.[index] ?? 0;
    return row;
  });

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer>
        <AreaChart
          data={rows}
          margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
          // Ruling U (i): the default stack offset ('none') accumulates
          // strictly in stack order regardless of sign, so a negative
          // residual only dips below zero if its magnitude exceeds every
          // appliance stacked beneath it -- never true for ordinary
          // attribution noise. 'sign' stacks positives above zero and
          // negatives below it, and is identical to 'none' whenever every
          // value is positive, so the common case is unaffected.
          stackOffset="sign"
        >
          <CartesianGrid stroke="var(--color-rule)" vertical={false} />
          <XAxis
            dataKey="ts"
            tickFormatter={(value: number) => formatClock(value)}
            stroke="var(--color-rule)"
            tick={{ fontSize: 11, fill: "var(--color-ink-muted)" }}
          />
          <YAxis
            unit=" W"
            stroke="var(--color-rule)"
            tick={{ fontSize: 11, fill: "var(--color-ink-muted)" }}
            // Ruling U (ii): stackOffset="sign" alone stacks the residual
            // below zero, but recharts still defaults a number axis'
            // domain to [0, 'auto'], which clips that band right back out
            // of view. The floor is Math.min(0, dataMin) rather than a
            // bare 0 so the axis keeps zero as its baseline in the
            // ordinary all-positive case (0 stays 0, nothing floats up)
            // and extends downward only when the data actually goes
            // negative -- a bare "auto" floor would instead float above
            // zero whenever everything is positive, losing the baseline.
            domain={[(dataMin: number) => Math.min(0, dataMin), "auto"]}
          />
          <Tooltip
            labelFormatter={(value: number) => formatClock(value)}
            formatter={(value: number, key: string) => [
              `${value.toFixed(1)} W`,
              key === RESIDUAL_KEY ? RESIDUAL_LABEL : key,
            ]}
          />
          {appliances.map((key, index) => {
            // noUncheckedIndexedAccess: the modulo is always in range, so
            // this fallback is unreachable at runtime - it exists so a
            // computed index into a plain array stays honestly typed as
            // `string`, not `string | undefined`, without a `!` assertion.
            const ink = APPLIANCE_INK[index % APPLIANCE_INK.length] ?? RESIDUAL_INK;
            return (
              // Step interpolation only. A smoothed line would imply the
              // system knows something between switching events.
              <Area
                key={key}
                type="stepAfter"
                dataKey={key}
                stackId="power"
                stroke={ink}
                fill={ink}
                fillOpacity={0.18}
                isAnimationActive={false}
              />
            );
          })}
          <Area
            type="stepAfter"
            dataKey={RESIDUAL_KEY}
            name={RESIDUAL_LABEL}
            stackId="power"
            stroke={RESIDUAL_INK}
            fill={RESIDUAL_INK}
            fillOpacity={0.35}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
