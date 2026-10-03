import { useState } from "react";
import {
  RESIDUAL_KEY,
  UNAVAILABLE_MARK,
  UNKNOWN_PREFIX,
} from "../../lib/constants";
import { formatRinggit, multiplyRinggit } from "../../lib/money";
import type { LedgerRow } from "../../lib/api";

type SortKey = "hours" | "kwh" | "ringgit" | "co2" | "name";
type Window = "today" | "week" | "month";

const WINDOWS: { id: Window; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "week", label: "This week" },
  { id: "month", label: "This month" },
];

const NO_BILL_TITLE =
  "Unavailable — the month bill this row's share is taken from has not loaded";
const NO_FACTOR_TITLE =
  "Unavailable — no emission factor has loaded for this month";

interface AppliancesProps {
  rows: LedgerRow[];
  window: Window;
  onWindowChange: (window: Window) => void;
  protectedIds: Set<string>;
  /**
   * `bill.total` from `/api/month`: the WHOLE month-to-date bill as a
   * money string. NOT a per-kWh rate -- each row is priced as its share
   * of this total, so the name says "total" and not "per kWh".
   * `null` when `/api/month` is unavailable.
   */
  monthBillTotalRinggit: string | null;
  /**
   * `bill.kwh` from the same payload: the month-to-date consumption that
   * `monthBillTotalRinggit` was computed from. The denominator of the
   * share, not a rate. `null` when `/api/month` is unavailable.
   */
  monthBillKwh: string | null;
  /** `null` when `/api/month` is unavailable. Never defaulted to "0". */
  kgCo2PerKwh: string | null;
}

function displayName(id: string): string {
  if (id === RESIDUAL_KEY) return "Unidentified";
  return id.replace(/_/g, " ");
}

export function Appliances({
  rows,
  window,
  onWindowChange,
  protectedIds,
  monthBillTotalRinggit,
  monthBillKwh,
  kgCo2PerKwh,
}: AppliancesProps) {
  const [sort, setSort] = useState<SortKey>("kwh");

  // Sarawak Energy's Tariff D is FLAT-BAND, not progressive-block: the
  // month's total consumption selects ONE rate and that rate applies to
  // every unit used that month (tariff/smartwatt_tariff/bill.py:9-10). So
  // every kWh in the month carries the same effective rate, and an
  // appliance's share of the bill IS what that appliance cost.
  //
  // Pricing a row as `share x bill.total` rather than `rate x kwh`
  // inherits the 25% discount, the RM 5.00 minimum charge and the service
  // tax automatically, because it starts from the real total. It is also
  // continuous across a band edge, where a rate-shaped figure is not.
  //
  // What must NOT be used here is `cliff.marginal`. That is a difference
  // of two whole bills (`evaluate(kwh + 1).total - evaluate(kwh).total`,
  // cliff.py:35-40), not a rate. Below ~28 kWh the minimum charge makes
  // it exactly "0.00" -- so for the first day or two of every billing
  // month it would price this entire column at RM 0.00 -- and within
  // 1 kWh of a band edge it jumps to the whole-month re-pricing, which
  // would price a 120 kWh row at roughly forty times the real bill.
  //
  // `bill.kwh` is a physical quantity (str(Decimal) kWh), so Number() on
  // it is correct. The money side never becomes a number: multiplyRinggit
  // takes the total as a string and multiplies in BigInt.
  //
  // The `window` selector may be today/week/month while this bill is
  // always month-to-date. That is correct, not a mismatch: the effective
  // rate is uniform across the month, so a single day's kWh divided by
  // the month's kWh and multiplied by the month's bill prices that day at
  // exactly the same rate the month is priced at.
  const monthKwh = monthBillKwh === null ? Number.NaN : Number(monthBillKwh);
  // Guarding zero (and a non-finite parse) is not defensive noise: at the
  // very start of a billing month mtd_kwh IS 0, and dividing by it would
  // put NaN or a fabricated RM 0.00 in the column. An unavailable figure
  // renders as unavailable.
  const billTotal =
    monthBillTotalRinggit !== null && Number.isFinite(monthKwh) && monthKwh !== 0
      ? monthBillTotalRinggit
      : null;

  const enriched = rows.map((row) => {
    const kwh = row.wh / 1000;
    return {
      ...row,
      kwh,
      hours: row.minutes / 60,
      ringgit: billTotal === null ? null : multiplyRinggit(billTotal, kwh / monthKwh),
      // kg CO2 is a physical quantity derived from a published emission
      // factor, not currency -- Number() here is fine, unlike above.
      co2: kgCo2PerKwh === null ? null : kwh * Number(kgCo2PerKwh),
    };
  });

  const sorted = [...enriched].sort((a, b) => {
    if (sort === "name") return a.appliance_id.localeCompare(b.appliance_id);
    if (sort === "hours") return b.hours - a.hours;
    // ringgit = kwh / monthKwh x billTotal, and monthKwh/billTotal are the
    // SAME two figures for every row in this ledger, so ordering by
    // ringgit is exactly ordering by kwh. Comparing the already-numeric
    // kwh field instead of parsing the `ringgit` money string back into a
    // float keeps this sort from ever coercing money for a comparison
    // (Ruling E) -- Number(a.ringgit) - Number(b.ringgit) would do exactly
    // that -- and it stays defined when `ringgit` is null.
    if (sort === "ringgit") return b.kwh - a.kwh;
    // Same argument: co2 = kwh x one non-negative factor shared by every
    // row, so this ordering IS the kWh ordering, and it survives a null
    // factor rather than comparing against a missing value.
    if (sort === "co2") return b.kwh - a.kwh;
    return b.kwh - a.kwh;
  });

  const header = (key: SortKey, label: string) => (
    <th className="px-3 py-2 text-right text-xs font-normal">
      <button
        type="button"
        onClick={() => setSort(key)}
        className={sort === key ? "underline" : "text-ink-muted"}
      >
        {label}
      </button>
    </th>
  );

  return (
    <section className="space-y-4">
      <div className="flex gap-4">
        {WINDOWS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => onWindowChange(entry.id)}
            className={
              window === entry.id
                ? "border-ink border-b-2 pb-1 text-xs"
                : "text-ink-muted pb-1 text-xs"
            }
          >
            {entry.label}
          </button>
        ))}
      </div>

      <table className="w-full">
        <thead className="rule-strong">
          <tr>
            <th className="px-3 py-2 text-left text-xs font-normal">
              <button
                type="button"
                onClick={() => setSort("name")}
                className={sort === "name" ? "underline" : "text-ink-muted"}
              >
                Appliance
              </button>
            </th>
            {header("hours", "Hours on")}
            {header("kwh", "kWh")}
            {header("ringgit", "Ringgit")}
            {header("co2", "kg CO₂e")}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr
              key={row.appliance_id}
              data-testid={`row-${row.appliance_id}`}
              className="rule-b"
            >
              <td className="px-3 py-2 text-sm">
                {displayName(row.appliance_id)}
                {protectedIds.has(row.appliance_id) && (
                  <span
                    data-testid="protected-mark"
                    title="Protected — this appliance can never be cut"
                    className="text-warn ml-2 text-[10px] uppercase tracking-wide"
                  >
                    protected · un-cuttable
                  </span>
                )}
                {row.appliance_id.startsWith(UNKNOWN_PREFIX) && (
                  <span className="text-ink-muted ml-2 text-[10px] uppercase tracking-wide">
                    unnamed load
                  </span>
                )}
              </td>
              <td data-testid="cell-hours" className="figure px-3 py-2 text-right text-sm">
                {row.hours.toFixed(1)}
              </td>
              <td data-testid="cell-kwh" className="figure px-3 py-2 text-right text-sm">
                {row.kwh.toFixed(1)}
              </td>
              <td
                data-testid="cell-ringgit"
                data-unavailable={row.ringgit === null ? "true" : "false"}
                // An unavailable cell is not an amount, so it does not
                // take the ringgit accent.
                className={`figure px-3 py-2 text-right text-sm ${
                  row.ringgit === null ? "text-ink-muted" : "text-ringgit"
                }`}
                title={row.ringgit === null ? NO_BILL_TITLE : undefined}
              >
                {row.ringgit === null
                  ? UNAVAILABLE_MARK
                  : formatRinggit(row.ringgit)}
              </td>
              <td
                data-testid="cell-co2"
                data-unavailable={row.co2 === null ? "true" : "false"}
                className={`figure px-3 py-2 text-right text-sm ${
                  row.co2 === null ? "text-ink-muted" : ""
                }`}
                title={row.co2 === null ? NO_FACTOR_TITLE : undefined}
              >
                {row.co2 === null ? UNAVAILABLE_MARK : `${row.co2.toFixed(2)} kg`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
