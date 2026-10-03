import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { MonthPayload } from "../src/lib/api";

const EXAMPLES = resolve(__dirname, "../../contract/examples");

/**
 * The ONLY source of payloads for component tests. A test that invents its
 * own payload shape is a test that can pass while the product is broken.
 */
export function loadExample<T = unknown>(name: string): T {
  return JSON.parse(readFileSync(resolve(EXAMPLES, `${name}.json`), "utf8"));
}

const PROVENANCE = {
  // Ruling AB: deliberately clean of the word "provisional" in either
  // field. The old fixture embedded "(Provisional)"/"(provisional)" in
  // source/vintage, which rendered unconditionally regardless of the
  // `provisional` flag -- so a test asserting merely "the word appears
  // somewhere" passed even with `{framing.provisional && ...}` deleted
  // entirely from the component. That word can only come from the flag now.
  source: "Energy Commission — GEF in Malaysia, 2022-2024",
  url: "https://www.st.gov.my/",
  vintage: "2024",
};

/**
 * A whole `/api/month` payload for a 341 kWh month inside the discount
 * window.
 *
 * `contract/examples/` carries telemetry, events and fingerprints but no
 * REST payloads (generating those from pytest is recorded as a
 * follow-up), so this is the one hand-written payload in the suite -- and
 * it lives here, shared, rather than being copied into each test file
 * that needs it, because a duplicated hand-derived payload is exactly how
 * the two copies drift.
 *
 * Every money figure below was re-derived by running smartwatt_tariff
 * against this consumption on a date in the discount window:
 *
 *   evaluate(341).display() -> energy 92.07, discount 23.02, total 69.05
 *   evaluate(417).display() -> energy 123.02, discount 30.75, total 92.26
 *   evaluate_undiscounted(341).total                        -> 92.07
 *   marginal(341)  = 69.2550 - 69.0525 = 0.2025             -> "0.20"
 *   band_step(400) = 88.50 - 81.00 = 7.50                   -> "7.50"
 *
 * An earlier copy carried marginal "10.30" / band_step "10.00" -- the
 * UNDISCOUNTED figures, from a different consumption -- beside
 * discount_active: true, which no single server response could produce.
 *
 * bill_projected's displayed fields deliberately do NOT add up by eye:
 * 123.02 - 30.75 = 92.27, but the server subtracts the UNROUNDED figures
 * and rounds once at the end, giving 92.26. Rounding each field to sen
 * and then reconciling them against each other is not what
 * `Bill.display()` does, and "correcting" 92.26 to 92.27 would be
 * inventing a figure to satisfy arithmetic the server never performs.
 */
export const MONTH_PAYLOAD: MonthPayload = {
  now_ts: 1_754_035_200,
  mtd_kwh: 341,
  projected_kwh: 417,
  bill: { kwh: "341", rate_sen: "27.0", energy: "92.07", service_tax: "0.00", discount: "23.02", total: "69.05" },
  bill_projected: { kwh: "417", rate_sen: "29.5", energy: "123.02", service_tax: "0.00", discount: "30.75", total: "92.26" },
  bill_undiscounted: { kwh: "341", rate_sen: "27.0", energy: "92.07", service_tax: "0.00", discount: "0.00", total: "92.07" },
  discount_active: true,
  cliff: {
    boundary_kwh: "400", kwh_remaining: "-17.000",
    current_rate_sen: "27.0", next_rate_sen: "29.5",
    band_step: "7.50", marginal: "0.20",
    culprit: { appliance_id: "incandescent_lamp", projected_kwh: "116.500", sufficient_alone: true },
  },
  carbon: {
    // provisional deliberately DIFFERS between the two factors (Ruling
    // AB) -- local false, hypothetical true -- so a test can tell the two
    // panels apart by the flag alone, not by shared fixture text.
    local: { region: "sarawak", kg: "67.859", hypothetical: false, provisional: false, factor_kg_per_kwh: "0.199", provenance: PROVENANCE },
    hypothetical: { region: "peninsular", kg: "252.340", hypothetical: true, provisional: true, factor_kg_per_kwh: "0.740", provenance: PROVENANCE },
    comparison: {
      ratio: "3.719",
      trend: { "2022": "0.769", "2023": "0.760", "2024": "0.740" },
      statement: "Sarawak's grid is already substantially cleaner than Peninsular Malaysia's.",
      displaced_framing: "SmartWatt does not publish a marginal-intensity figure.",
    },
    tree_years: null,
  },
};

/** `tariff/smartwatt_tariff/config/tariff.toml`, the published schedule. */
const TARIFF_TOML = resolve(
  __dirname,
  "../../tariff/smartwatt_tariff/config/tariff.toml",
);

/** One `[[bands]]` entry, exactly as the TOML spells it. */
export interface TariffTomlBand {
  /** `null` for the final, open-ended band, which declares no upper bound. */
  up_to_kwh: string | null;
  sen_per_kwh: string;
}

/**
 * Reads the `[[bands]]` array out of the published tariff schedule.
 *
 * Regex rather than a TOML parser on purpose: this is the only TOML the
 * dashboard ever reads, and adding a dependency to a browser bundle's
 * test suite to parse ten fixed key/value pairs is not a trade worth
 * making. Values stay STRINGS all the way out -- the TOML deliberately
 * quotes every figure ("TOML has native floats, and a float that reaches
 * a bill path defeats the Decimal discipline entirely"), and re-widening
 * them here to compare would throw that away.
 */
export function loadTariffBands(): TariffTomlBand[] {
  const toml = readFileSync(TARIFF_TOML, "utf8");
  return (
    toml
      .split(/^\[\[bands\]\][ \t]*$/m)
      .slice(1)
      // Everything after the next section header ([charges], [discount])
      // belongs to that section, not to this band.
      .map((chunk) => chunk.split(/^\[/m)[0] ?? "")
      .map((chunk) => {
        const sen = /^sen_per_kwh\s*=\s*"([^"]+)"/m.exec(chunk);
        if (sen?.[1] === undefined) {
          throw new Error(`tariff.toml: a [[bands]] entry has no sen_per_kwh`);
        }
        const upTo = /^up_to_kwh\s*=\s*"([^"]+)"/m.exec(chunk);
        return { up_to_kwh: upTo?.[1] ?? null, sen_per_kwh: sen[1] };
      })
  );
}
