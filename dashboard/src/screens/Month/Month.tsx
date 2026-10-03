import { useState } from "react";
import { CliffGauge } from "../../components/CliffGauge/CliffGauge";
import { Figure } from "../../components/Figure";
import type {
  ApplianceRow,
  CarbonFraming,
  MonthPayload,
  WhatIfResult,
} from "../../lib/api";

interface MonthProps {
  month: MonthPayload;
  onWhatIf: (applianceId: string, hours: number) => void;
  /** The appliance roster from `/api/appliances`, for the what-if picker. */
  appliances?: ApplianceRow[];
  whatIf?: WhatIfResult | null;
  /**
   * Set when the last what-if request failed. Rendered explicitly: a
   * panel that simply does not appear is indistinguishable from "not
   * calculated yet", which hides a real failure.
   */
  whatIfError?: string | null;
}

function Provenance({ framing }: { framing: CarbonFraming }) {
  return (
    <div className="text-ink-muted pt-1 text-[10px] leading-snug">
      {framing.provenance.source} ·{" "}
      <a href={framing.provenance.url} className="underline">
        {framing.provenance.url}
      </a>{" "}
      · {framing.provenance.vintage}
      {framing.provisional && (
        <span className="text-warn"> · provisional</span>
      )}
    </div>
  );
}

export function Month({
  month,
  onWhatIf,
  appliances = [],
  whatIf = null,
  whatIfError = null,
}: MonthProps) {
  const [applianceId, setApplianceId] = useState("");
  const [hours, setHours] = useState(10);
  const { carbon } = month;

  // US25/26 say "choose an appliance". A free-text box asked the user to
  // recall a raw appliance_id from memory, and any typo came back as a
  // 404. The roster carries the authoritative display_name, so the
  // options are labelled with it and valued with the id.
  //
  // Falling back to the first roster entry keeps the control's state and
  // what the user can see agreeing with each other: an uncontrolled
  // <select> shows its first option before anything is chosen, so
  // submitting the empty initial state would ask for an appliance the
  // user never saw named.
  const selectedId = applianceId || appliances[0]?.id || "";
  const rosterEmpty = appliances.length === 0;

  return (
    <section className="space-y-8">
      <CliffGauge
        mtdKwh={month.mtd_kwh}
        projectedKwh={month.projected_kwh}
        cliff={month.cliff}
      />

      <div className="grid grid-cols-5 gap-8">
        <Figure label="Month to date" value={month.mtd_kwh} unit="kWh" dp={0} />
        <Figure label="Projected" value={month.projected_kwh} unit="kWh" dp={0} />
        <Figure label="Bill now" value={month.bill.total} money />
        {/* Ruling AC: US19/US20 cover kWh AND bill for the projection, not
            kWh alone -- withholding "417 kWh, which will cost about
            RM 92.26" and showing only the bare kWh number holds back the
            figure a bill-shock product exists to surface. */}
        <Figure
          label="Projected bill"
          value={month.bill_projected.total}
          money
        />
        <Figure
          label="Without discount"
          value={month.bill_undiscounted.total}
          money
        />
      </div>
      {month.discount_active && (
        <p className="text-ink-muted -mt-6 text-xs">
          A 25% state discount is applied to the total, April–December 2026.
          The right-hand figure is what you will pay once it ends.
        </p>
      )}

      <div className="rule-t pt-4">
        <h2 className="pb-2 text-sm font-semibold">What if I stopped?</h2>
        <div className="flex items-end gap-4">
          <label className="text-xs" htmlFor="whatif-appliance">
            What if I stopped using
            <select
              id="whatif-appliance"
              value={selectedId}
              disabled={rosterEmpty}
              onChange={(e) => setApplianceId(e.target.value)}
              className="figure border-rule ml-2 border-b bg-transparent px-1"
            >
              {rosterEmpty ? (
                <option value="">no appliances loaded</option>
              ) : (
                appliances.map((appliance) => (
                  <option key={appliance.id} value={appliance.id}>
                    {appliance.display_name}
                  </option>
                ))
              )}
            </select>
          </label>
          <label className="text-xs" htmlFor="whatif-hours">
            for
            <input
              id="whatif-hours"
              type="number"
              value={hours}
              onChange={(e) => setHours(Number(e.target.value))}
              className="figure border-rule ml-2 w-16 border-b bg-transparent px-1"
            />
            hours
          </label>
          <button
            type="button"
            disabled={rosterEmpty}
            onClick={() => onWhatIf(selectedId, hours)}
            className="border-ink border px-3 py-1 text-xs disabled:opacity-40"
          >
            Calculate
          </button>
        </div>

        {whatIfError !== null && (
          <p data-testid="whatif-error" className="text-warn pt-4 text-xs">
            {whatIfError}
          </p>
        )}

        {whatIf && (
          <div data-testid="whatif-result" className="grid grid-cols-3 gap-8 pt-4">
            <Figure label="Naive saving" value={whatIf.naive_saving} money />
            <Figure label="True saving" value={whatIf.true_saving} money />
            <div>
              {/* `multiple` is a RATIO, not an amount. Labelling it
                  "Difference" read as an absolute number of ringgit --
                  the one thing it is not. The label and the value now
                  read as one sentence: "True saving is 3.719x the
                  naive". */}
              <div className="text-ink-muted text-xs uppercase tracking-wide">
                True saving is
              </div>
              <div data-testid="whatif-multiple" className="figure text-2xl">
                {whatIf.multiple}× the naive
              </div>
              {whatIf.crosses_back && (
                <div data-testid="whatif-crosses-back" className="text-warn text-xs">
                  This saving drops you back under a band boundary, which
                  re-prices the whole month.
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="bg-carbon-field space-y-4 p-6">
        <h2 className="text-sm font-semibold">Carbon</h2>

        <div data-testid="carbon-local">
          <div className="text-ink-muted text-xs uppercase tracking-wide">
            Your impact — Sarawak grid
          </div>
          <div className="figure text-2xl">{carbon.local.kg} kg CO₂e</div>
          <Provenance framing={carbon.local} />
        </div>

        <div data-testid="carbon-hypothetical" className="rule-t pt-4">
          <div className="text-ink-muted text-xs uppercase tracking-wide">
            Hypothetical — the same consumption on the Peninsular grid
          </div>
          <div className="figure text-2xl">
            {carbon.hypothetical.kg} kg CO₂e
          </div>
          <Provenance framing={carbon.hypothetical} />
        </div>

        <div data-testid="carbon-comparison" className="rule-t pt-4 text-sm">
          <p>
            <span className="figure">{carbon.comparison.ratio}×</span> the local
            figure. Peninsular Malaysia fell{" "}
            {Object.entries(carbon.comparison.trend)
              .map(([year, value]) => `${value} (${year})`)
              .join(" → ")}{" "}
            while Sarawak held flat, so this comparison depreciates each year.
          </p>
          <p className="pt-2">{carbon.comparison.statement}</p>
          <p className="text-ink-muted pt-2 text-xs">
            {carbon.comparison.displaced_framing}
          </p>
        </div>

        {carbon.tree_years !== null && (
          <div data-testid="tree-years" className="rule-t pt-4 text-sm">
            Equivalent to <span className="figure">{carbon.tree_years}</span>{" "}
            mature-tree-years of absorption.
          </div>
        )}
      </div>
    </section>
  );
}
