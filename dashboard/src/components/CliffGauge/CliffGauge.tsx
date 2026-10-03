import { formatRinggit } from "../../lib/money";
import type { MonthPayload } from "../../lib/api";

/** The published Tariff D schedule, for drawing the staircase only. */
export const DEFAULT_BANDS: { up_to: number | null; sen: number }[] = [
  { up_to: 100, sen: 18.0 },
  { up_to: 150, sen: 18.0 },
  { up_to: 200, sen: 22.0 },
  { up_to: 300, sen: 25.0 },
  { up_to: 400, sen: 27.0 },
  { up_to: 500, sen: 29.5 },
  { up_to: 700, sen: 30.0 },
  { up_to: 800, sen: 30.5 },
  { up_to: 1300, sen: 31.0 },
  { up_to: null, sen: 31.5 },
];

type VisibleBand = (typeof DEFAULT_BANDS)[number] & { from: number; to: number };

/**
 * Which bands are visible on the shared kWh axis, and where each one
 * starts/ends there.
 *
 * A band belongs on the axis when its LOWER edge sits inside the visible
 * range -- not, as `band.up_to <= axisMax` reads at a glance, when its
 * UPPER edge does. That obvious-looking filter is wrong two ways: it
 * admits the open-ended band unconditionally (`null ?? axisMax` is
 * always `<= axisMax`), and it drops any real band whose upper edge
 * merely exceeds axisMax even though the axis cuts straight through the
 * middle of it -- which then silently lets the open-ended band paint
 * the clipped remainder at ITS rate instead. That is exactly the
 * misrepresentation this gauge exists to prevent: a real Tariff D band
 * rendered at the wrong sen/kWh, in precisely the near-boundary state
 * the gauge is for. (Ruling W.)
 *
 * A band's lower edge is the PREVIOUS band's `up_to` (0 for the first
 * band), so it must be read off each band's position in the full
 * `bands` array before anything is dropped -- recovering the
 * predecessor from an already-filtered array would pair band i with the
 * wrong neighbour once earlier bands go missing. So this maps first
 * (still walking the untouched `bands` array, index intact) to attach
 * each band's true `from`/`to`, clamping `to` at axisMax, and only then
 * filters on `from`. A partially-visible band therefore draws only the
 * rate that is actually true within the visible window, never
 * extrapolating a band the axis hasn't reached.
 *
 * Exported and kept pure (plain `bands`/`axisMax` arguments, no JSX) so
 * the test suite can pin this arithmetic directly instead of only
 * observing its DOM shadow: a DOM-only check like "more than 3 steps
 * rendered" cannot tell this implementation apart from the one it
 * replaced -- both render 6 steps for the OVER fixture, one of them
 * ending on the wrong rate.
 */
export function visibleBands(
  bands: typeof DEFAULT_BANDS,
  axisMax: number,
): VisibleBand[] {
  return bands
    .map((band, index) => ({
      ...band,
      // index > 0 here always has a predecessor at index - 1, so the
      // array access is in range; `!` just says so to the type checker.
      from: index === 0 ? 0 : (bands[index - 1]!.up_to ?? 0),
      to: Math.min(band.up_to ?? axisMax, axisMax),
    }))
    .filter((band) => band.from < axisMax);
}

interface CliffGaugeProps {
  mtdKwh: number;
  projectedKwh: number;
  cliff: MonthPayload["cliff"];
  bands?: typeof DEFAULT_BANDS;
}

/**
 * Two stacked rows over one shared kWh axis.
 *
 *   rate                    +------- 29.5 -------
 *   (sen/kWh)   --- 27.0 ---+
 *               ----------------------------------  <- a staircase, not a ramp
 *                           ^ 400 kWh
 *   consumption  *----------|--o
 *                MTD        |  projected
 *
 * The top row is what makes "flat-band, not progressive-block" visible
 * without a paragraph of explanation.
 */
export function CliffGauge({
  mtdKwh,
  projectedKwh,
  cliff,
  bands = DEFAULT_BANDS,
}: CliffGaugeProps) {
  const boundary = cliff.boundary_kwh === null ? null : Number(cliff.boundary_kwh);
  const over = boundary !== null && projectedKwh > boundary;
  const state = boundary === null ? "no-boundary" : over ? "over" : "clear";

  const axisMax = Math.max(projectedKwh, boundary ?? 0, mtdKwh) * 1.15 || 100;
  const pct = (kwh: number) => `${Math.min(100, (kwh / axisMax) * 100)}%`;

  // See visibleBands() above (Ruling W) for why the visible set isn't the
  // one-line `up_to <= axisMax` filter it looks like it should be. Pulled
  // out to a standalone function so the test suite can assert on the
  // real arithmetic, not just its DOM shadow.
  const visible = visibleBands(bands, axisMax);
  const minSen = Math.min(...visible.map((b) => b.sen));
  const maxSen = Math.max(...visible.map((b) => b.sen), minSen + 1);

  return (
    <section
      data-testid="cliff-gauge"
      data-state={state}
      className="rule-t rule-b py-6"
    >
      {/* Top row: rate as a step function of consumption. */}
      <div className="relative mb-1 h-16 w-full" aria-hidden>
        {visible.map((band) => {
          const height = ((band.sen - minSen) / (maxSen - minSen)) * 100;
          return (
            <div
              key={`${band.up_to}-${band.sen}`}
              data-testid={`rate-step-${band.up_to ?? "open"}`}
              className="absolute bottom-0 border-t-2 border-ink"
              style={{
                left: pct(band.from),
                width: `calc(${pct(band.to)} - ${pct(band.from)})`,
                height: `${Math.max(4, height)}%`,
              }}
            />
          );
        })}
      </div>
      <div className="text-ink-muted mb-4 text-[10px] uppercase tracking-wide">
        rate, sen per kWh — a staircase, not a ramp
      </div>

      {/* Bottom row: where you are, where you land, where the edge is. */}
      <div className="relative h-10 w-full border-t border-rule">
        <div
          className="absolute top-0 h-4 border-l-2 border-ink"
          style={{ left: pct(mtdKwh) }}
        />
        <div
          className="absolute top-0 h-4 border-l-2 border-dashed border-ink-muted"
          style={{ left: pct(projectedKwh) }}
        />
        {boundary !== null && (
          <div
            data-testid="gauge-boundary-rule"
            className={`absolute top-0 h-10 border-l-2 ${
              over ? "border-ringgit" : "border-rule-strong"
            }`}
            style={{ left: pct(boundary) }}
          />
        )}
      </div>

      <div className="figure flex justify-between pt-1 text-xs">
        <span data-testid="marker-mtd">{mtdKwh.toFixed(0)} kWh so far</span>
        {boundary !== null && (
          <span data-testid="gauge-boundary">{boundary} kWh boundary</span>
        )}
        <span data-testid="marker-projected">
          {projectedKwh.toFixed(0)} kWh projected
        </span>
      </div>

      <div className="grid grid-cols-3 gap-8 pt-6">
        {cliff.kwh_remaining !== null && (
          <div>
            <div className="text-ink-muted text-xs uppercase tracking-wide">
              Room left
            </div>
            <div data-testid="kwh-remaining" className="figure text-xl">
              {Number(cliff.kwh_remaining).toFixed(0)} kWh
            </div>
          </div>
        )}
        <div>
          <div className="text-ink-muted text-xs uppercase tracking-wide">
            Re-prices your whole month
          </div>
          <div data-testid="band-step" className="figure text-ringgit text-xl">
            {formatRinggit(cliff.band_step)}
          </div>
        </div>
        <div>
          <div className="text-ink-muted text-xs uppercase tracking-wide">
            The next unit costs
          </div>
          <div data-testid="marginal" className="figure text-ringgit text-xl">
            {formatRinggit(cliff.marginal)}
          </div>
        </div>
      </div>

      {over && cliff.culprit && (
        <p data-testid="culprit" className="pt-4 text-sm">
          <span className="font-semibold">{cliff.culprit.appliance_id}</span> is
          the largest projected contributor at {cliff.culprit.projected_kwh} kWh
          {cliff.culprit.sufficient_alone
            ? " — stopping it alone would bring you back under."
            : " — but it is not enough on its own to bring you back under."}
        </p>
      )}
    </section>
  );
}
