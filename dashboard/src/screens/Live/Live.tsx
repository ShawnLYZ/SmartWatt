import { Figure } from "../../components/Figure";
import { SourceBadge } from "../../components/SourceBadge";
import type { SeriesPayload } from "../../lib/api";
import type { Telemetry } from "../../types/contract";
import { RESIDUAL_LABEL, StackedPower } from "./StackedPower";

const NO_MONTH_TITLE =
  "Unavailable — /api/month has not returned, so there is no rate or factor to derive this from";

export interface LiveProps {
  telemetry: Telemetry | null;
  stale: boolean;
  /**
   * `null` when `/api/series` is unavailable. The 15-minute chart is a
   * REST poll, and a failed poll must not leave the previous window on
   * screen indefinitely with nothing saying so.
   */
  series: SeriesPayload | null;
  /** From /api/month. The MARGINAL cost of the next kWh, not the nominal
   *  rate - US3. Arrives as a string and stays one. `null` when
   *  /api/month is unavailable; never substituted with "0.00". */
  marginalRinggitPerHour: string | null;
  /** `null` when the carbon factor is unavailable; never substituted with 0. */
  gramsCo2PerHour: number | null;
}

export function Live({
  telemetry,
  stale,
  series,
  marginalRinggitPerHour,
  gramsCo2PerHour,
}: LiveProps) {
  // The connection/staleness indicator lives in App's header now, so it
  // is present on all three screens (spec, Data flow: "A single
  // reconnecting-socket hook owns connection state and every screen reads
  // it"). It is deliberately NOT repeated here or in the empty state
  // below: two elements carrying the same testid is a duplicate the
  // header already covers, not extra honesty.
  if (telemetry === null) {
    return (
      <div className="text-ink-muted py-16 text-center text-sm">
        Waiting for telemetry…
      </div>
    );
  }

  const { electrical, attribution } = telemetry;
  // US8's live "Unidentified" figure sits in the sensor strip beside Vrms
  // and frequency, which are 1 Hz telemetry. It must therefore come from
  // the 1 Hz telemetry too. Reading it from `series` instead put the
  // per-minute ledger rollup -- fetched by a 30 s REST poll, so up to
  // 60-90 s old -- next to genuinely live figures under a staleness clock
  // that only ever tracked the socket, making stale data look fresh.
  const residualNow = attribution.residual_w;

  return (
    <section className="space-y-8">
      <div className="flex items-start justify-between">
        <div className="grid flex-1 grid-cols-3 gap-8">
          <Figure label="Power" value={electrical.p} unit="W" dp={0} stale={stale} />
          <Figure
            label="Cost per hour"
            value={marginalRinggitPerHour}
            money
            stale={stale}
            unavailableTitle={NO_MONTH_TITLE}
          />
          <Figure
            label="CO₂ per hour"
            value={gramsCo2PerHour}
            unit="g"
            dp={0}
            stale={stale}
            unavailableTitle={NO_MONTH_TITLE}
          />
        </div>
        <SourceBadge source={telemetry.source} />
      </div>

      <div className="rule-t pt-4">
        <div className="flex items-baseline justify-between pb-2">
          <h2 className="text-sm font-semibold">Last 15 minutes</h2>
          <span className="text-ink-muted text-xs">{RESIDUAL_LABEL} shown</span>
        </div>
        {series === null ? (
          <div
            data-testid="series-unavailable"
            className="text-ink-muted flex h-64 items-center justify-center text-sm"
          >
            The last 15 minutes are not available right now.
          </div>
        ) : (
          /* Ruling D: the chart is driven by the per-minute ledger axis,
             not the 1 Hz telemetry axis - `series.series` is indexed
             against `t_min`, and indexing it against `t` would draw ~15
             real values followed by ~885 zeros under the wrong timestamps. */
          <StackedPower t={series.t_min} series={series.series} />
        )}
      </div>

      <div className="rule-t grid grid-cols-4 gap-8 pt-4">
        <Figure label="Voltage" value={electrical.vrms} unit="V" stale={stale} />
        <Figure
          label="Frequency"
          value={electrical.freq}
          unit="Hz"
          dp={2}
          stale={stale}
        />
        <Figure
          label="Detection floor"
          value={attribution.floor_w}
          unit="W"
          stale={stale}
        />
        <div data-testid="residual-current">
          <Figure
            label={RESIDUAL_LABEL}
            value={residualNow}
            unit="W"
            stale={stale}
          />
        </div>
      </div>
    </section>
  );
}
