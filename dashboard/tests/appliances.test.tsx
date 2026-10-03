import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Appliances } from "../src/screens/Appliances/Appliances";
import type { LedgerRow } from "../src/lib/api";

const ROWS: LedgerRow[] = [
  { appliance_id: "kettle", wh: 120_000, w_mean: 1800, minutes: 400 },
  { appliance_id: "incandescent_lamp", wh: 95_000, w_mean: 40, minutes: 1200 },
  { appliance_id: "laptop_charger", wh: 30_000, w_mean: 65, minutes: 900 },
  { appliance_id: "unknown_1", wh: 12_000, w_mean: 305, minutes: 120 },
  { appliance_id: "__residual__", wh: -400, w_mean: -2, minutes: 1400 },
];

// A real `bill.display()` pair for 341 kWh on a date inside the discount
// window, verified by running tariff/smartwatt_tariff/bill.py:
//   evaluate(Decimal("341"), date(2026, 8, 1)).display()
//     -> {'kwh': '341', 'rate_sen': '27.0', 'energy': '92.07',
//         'service_tax': '0.00', 'discount': '23.02', 'total': '69.05'}
// The total therefore already carries the 25% discount and the flat-band
// 27 sen rate, which is the whole reason a row is priced as a share of it
// rather than by multiplying some rate figure back out.
const BILL_TOTAL = "69.05";
const BILL_KWH = "341";

const render_ = (
  rows = ROWS,
  overrides: Partial<Parameters<typeof Appliances>[0]> = {},
) =>
  render(
    <Appliances
      rows={rows}
      window="month"
      onWindowChange={vi.fn()}
      protectedIds={new Set(["laptop_charger"])}
      monthBillTotalRinggit={BILL_TOTAL}
      monthBillKwh={BILL_KWH}
      kgCo2PerKwh="0.199"
      {...overrides}
    />,
  );

describe("Appliances", () => {
  it("renders a row per appliance", () => {
    render_();
    expect(screen.getAllByRole("row")).toHaveLength(ROWS.length + 1);
  });

  it("renders hours, kWh, ringgit and kg CO2 per row", () => {
    render_();
    const row = screen.getByTestId("row-kettle");
    expect(within(row).getByTestId("cell-hours")).toHaveTextContent("6.7");
    expect(within(row).getByTestId("cell-kwh")).toHaveTextContent("120.0");
    // The COMPUTED figure, not merely "RM". 120 kWh of a 341 kWh month
    // billed at RM 69.05 is 69.05 x 120/341 = 24.29912... -> RM 24.30.
    // Asserting only that "RM" appears is decorative: it passed just as
    // happily when the column was priced from cliff.marginal and read
    // RM 1,236.00 for this row.
    expect(within(row).getByTestId("cell-ringgit")).toHaveTextContent(
      "RM 24.30",
    );
    // 120 kWh x 0.199 kg/kWh.
    expect(within(row).getByTestId("cell-co2")).toHaveTextContent("23.88 kg");
  });

  it("prices every row at the same effective rate, as a share of the real bill", () => {
    render_();
    // 69.05 x 30/341 = RM 6.0748 -> RM 6.07. A quarter of the kettle's
    // kWh prices at a quarter of the kettle's ringgit, which is what
    // "flat-band, one rate for the whole month" means.
    expect(
      within(screen.getByTestId("row-laptop_charger")).getByTestId(
        "cell-ringgit",
      ),
    ).toHaveTextContent("RM 6.07");
  });

  it("prices a negative residual as a negative share rather than dropping it", () => {
    render_();
    // 69.05 x -0.4/341 = -RM 0.0810 -> -RM 0.08.
    expect(
      within(screen.getByTestId("row-__residual__")).getByTestId(
        "cell-ringgit",
      ),
    ).toHaveTextContent("-RM 0.08");
  });

  it("never prices a row from a rate-shaped figure", () => {
    // Regression pin for the Critical finding. cliff.marginal for this
    // month is "0.20" at 341 kWh and "7.72" within 1 kWh of the 400 kWh
    // edge (both verified by running smartwatt_tariff.cliff.marginal);
    // neither is a per-kWh rate. 120 x 0.20 = RM 24.00 and
    // 120 x 7.72 = RM 926.40 are the two figures the old wiring produced.
    // The correct share, RM 24.30, is close enough to the first to look
    // right and nowhere near the second -- which is exactly why this
    // needs pinning rather than eyeballing.
    render_();
    const cell = within(screen.getByTestId("row-kettle")).getByTestId(
      "cell-ringgit",
    );
    expect(cell).not.toHaveTextContent("RM 24.00");
    expect(cell).not.toHaveTextContent("RM 926.40");
    expect(cell).toHaveTextContent("RM 24.30");
  });

  it("renders the RM column unavailable, never RM 0.00, when the month kWh is zero", () => {
    // The first day or two of every billing month: mtd_kwh is 0 and the
    // bill is the RM 5.00 minimum charge. Dividing by zero here would
    // produce NaN, and substituting RM 0.00 would assert that every
    // appliance cost nothing -- the same invented-figure defect in a new
    // place. It must read as unavailable.
    render_(ROWS, { monthBillKwh: "0", monthBillTotalRinggit: "5.00" });
    for (const cell of screen.getAllByTestId("cell-ringgit")) {
      expect(cell).toHaveAttribute("data-unavailable", "true");
      expect(cell).not.toHaveTextContent("RM");
      expect(cell).toHaveTextContent("—");
    }
    // The kWh column is measured, not derived from the bill, so it is
    // still real and still rendered.
    expect(
      within(screen.getByTestId("row-kettle")).getByTestId("cell-kwh"),
    ).toHaveTextContent("120.0");
  });

  it("renders the RM column unavailable when the bill itself is unavailable", () => {
    render_(ROWS, { monthBillTotalRinggit: null, monthBillKwh: null });
    const cell = within(screen.getByTestId("row-kettle")).getByTestId(
      "cell-ringgit",
    );
    expect(cell).toHaveAttribute("data-unavailable", "true");
    expect(cell).not.toHaveTextContent("RM");
  });

  it("renders the CO2 column unavailable rather than zero when no factor has loaded", () => {
    render_(ROWS, { kgCo2PerKwh: null });
    const cell = within(screen.getByTestId("row-kettle")).getByTestId(
      "cell-co2",
    );
    expect(cell).toHaveAttribute("data-unavailable", "true");
    expect(cell).not.toHaveTextContent("0.00");
    expect(cell).toHaveTextContent("—");
  });

  it("always renders the residual as its own row", () => {
    render_();
    expect(screen.getByTestId("row-__residual__")).toBeInTheDocument();
  });

  it("labels the residual row Unidentified", () => {
    render_();
    expect(
      within(screen.getByTestId("row-__residual__")).getByText(/unidentified/i),
    ).toBeInTheDocument();
  });

  it("renders a negative residual without dropping it", () => {
    render_();
    expect(
      within(screen.getByTestId("row-__residual__")).getByTestId("cell-kwh"),
    ).toHaveTextContent("-0.4");
  });

  it("renders unknown loads with their energy", () => {
    render_();
    const row = screen.getByTestId("row-unknown_1");
    expect(within(row).getByTestId("cell-kwh")).toHaveTextContent("12.0");
    expect(within(row).getByText(/unnamed load/i)).toBeInTheDocument();
  });

  it("marks protected appliances un-cuttable", () => {
    render_();
    expect(
      within(screen.getByTestId("row-laptop_charger")).getByTestId(
        "protected-mark",
      ),
    ).toBeInTheDocument();
  });

  it("does not mark unprotected appliances", () => {
    render_();
    expect(
      within(screen.getByTestId("row-kettle")).queryByTestId("protected-mark"),
    ).not.toBeInTheDocument();
  });

  it("sorts by kWh descending by default", () => {
    render_();
    const ids = screen
      .getAllByTestId(/^row-/)
      .map((row) => row.getAttribute("data-testid"));
    expect(ids[0]).toBe("row-kettle");
  });

  it("re-sorts when a header is clicked", async () => {
    render_();
    await userEvent.click(screen.getByRole("button", { name: /hours/i }));
    const first = screen.getAllByTestId(/^row-/)[0];
    expect(first).toHaveAttribute("data-testid", "row-__residual__");
  });

  it("offers today, week and month windows", () => {
    render_();
    for (const label of ["Today", "This week", "This month"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("calls back on window change", async () => {
    const onWindowChange = vi.fn();
    render_(ROWS, { onWindowChange });
    await userEvent.click(screen.getByRole("button", { name: "Today" }));
    expect(onWindowChange).toHaveBeenCalledWith("today");
  });

  it("renders the residual row even on an otherwise empty ledger", () => {
    render_([{ appliance_id: "__residual__", wh: 0, w_mean: 0, minutes: 0 }]);
    expect(screen.getByTestId("row-__residual__")).toBeInTheDocument();
  });
});
