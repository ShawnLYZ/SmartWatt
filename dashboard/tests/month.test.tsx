import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Month } from "../src/screens/Month/Month";
import { MONTH_PAYLOAD as MONTH } from "./fixtures";
import type { ApplianceRow, MonthPayload, WhatIfResult } from "../src/lib/api";

const ROSTER: ApplianceRow[] = [
  { id: "incandescent_lamp", display_name: "Incandescent lamp", protected: 0, heating: 0, plug_device: null },
  { id: "kettle", display_name: "Kettle", protected: 0, heating: 1, plug_device: null },
  { id: "fridge", display_name: "Fridge", protected: 1, heating: 0, plug_device: null },
];

// Real whatif_payload() output, from smartwatt_tariff.whatif.whatif at a
// projection of 417 kWh saving 20 kWh on a date inside the discount
// window. The saving drops the household from 417 back to 397, under the
// 400 kWh boundary, so the whole month re-prices at 27 sen instead of
// 29.5 -- which is why the true saving is roughly twice the naive one.
const WHATIF_CROSSES: WhatIfResult = {
  appliance_id: "incandescent_lamp",
  hours: 10,
  kwh_saved: "20.000",
  naive_saving: "5.90",
  true_saving: "11.87",
  crosses_back: true,
  multiple: "2.012",
};

// The same call for a 5 kWh saving: 412 kWh is still in the 500 band, so
// nothing re-prices and the true saving is LESS than the naive one (the
// naive figure ignores the 25% discount). Both directions of the gap are
// real; the panel must not imply the true figure is always the larger.
const WHATIF_NO_CROSS: WhatIfResult = {
  appliance_id: "kettle",
  hours: 2,
  kwh_saved: "5.000",
  naive_saving: "1.48",
  true_saving: "1.11",
  crosses_back: false,
  multiple: "0.750",
};

const render_ = (
  overrides: Partial<MonthPayload> = {},
  props: Partial<Parameters<typeof Month>[0]> = {},
) =>
  render(
    <Month
      month={{ ...MONTH, ...overrides }}
      onWhatIf={vi.fn()}
      appliances={ROSTER}
      {...props}
    />,
  );

describe("Month", () => {
  it("renders the Cliff Gauge", () => {
    render_();
    expect(screen.getByTestId("cliff-gauge")).toBeInTheDocument();
  });

  it("renders month-to-date and projection", () => {
    render_();
    expect(screen.getByTestId("figure-month-to-date")).toHaveTextContent("341");
    expect(screen.getByTestId("figure-projected")).toHaveTextContent("417");
  });

  it("renders discounted and undiscounted side by side", () => {
    render_();
    expect(screen.getByTestId("figure-bill-now")).toHaveTextContent("RM 69.05");
    expect(screen.getByTestId("figure-without-discount")).toHaveTextContent(
      "RM 92.07",
    );
  });

  it("renders the projected bill as money, not just the projected kWh", () => {
    render_();
    // Ruling AC: month_payload() (server-side, month.py:166) computes the
    // full projected bill for exactly this reason -- kWh alone tells a
    // reader how much they'll use, not what it costs.
    expect(screen.getByTestId("figure-projected-bill")).toHaveTextContent(
      "RM 92.26",
    );
  });

  it("names the discount window", () => {
    render_();
    expect(screen.getByText(/25%/)).toBeInTheDocument();
  });

  it("labels the Peninsular figure hypothetical", () => {
    render_();
    const panel = screen.getByTestId("carbon-hypothetical");
    expect(panel).toHaveTextContent(/hypothetical/i);
  });

  it("never renders the hypothetical figure unlabelled", () => {
    render_();
    // Ruling I: the component renders `{carbon.hypothetical.kg} kg CO₂e`,
    // so the element's full text is "252.340 kg CO₂e" -- an exact-string
    // matcher for "252.340 kg" cannot match a substring. `kg CO₂e` is the
    // correct unit per spec, so the assertion is fixed, not the component.
    expect(screen.getByText(/252\.340 kg/)).toBeInTheDocument();
    expect(screen.getByTestId("carbon-hypothetical")).toHaveTextContent(
      /hypothetical/i,
    );
  });

  it("renders provenance for every factor", () => {
    render_();
    expect(screen.getAllByText(/Energy Commission/).length).toBeGreaterThan(1);
    expect(screen.getAllByRole("link").length).toBeGreaterThan(0);
  });

  it("renders the provisional label only for the factor whose flag is set", () => {
    render_();
    // Fixture: local.provisional is false, hypothetical.provisional is
    // true, and neither provenance string contains the word "provisional"
    // -- so this can only pass if the component actually reads the flag
    // per panel, not merely echoes fixture text.
    expect(screen.getByTestId("carbon-local")).not.toHaveTextContent(
      /provisional/i,
    );
    expect(screen.getByTestId("carbon-hypothetical")).toHaveTextContent(
      /provisional/i,
    );
  });

  it("renders the comparison ratio and trend", () => {
    render_();
    expect(screen.getByTestId("carbon-comparison")).toHaveTextContent("3.719");
    expect(screen.getByTestId("carbon-comparison")).toHaveTextContent("0.740");
  });

  it("states the grid is already cleaner", () => {
    render_();
    expect(screen.getByText(/already substantially cleaner/i)).toBeInTheDocument();
  });

  it("renders the displaced framing qualitatively", () => {
    render_();
    expect(
      screen.getByText(/does not publish a marginal-intensity figure/i),
    ).toBeInTheDocument();
  });

  it("omits the tree-year line entirely when unsourced", () => {
    render_();
    expect(screen.queryByTestId("tree-years")).not.toBeInTheDocument();
    expect(screen.queryByText(/tree/i)).not.toBeInTheDocument();
  });

  it("renders the tree-year line when supplied", () => {
    render_({
      carbon: { ...MONTH.carbon, tree_years: "3.117" },
    });
    expect(screen.getByTestId("tree-years")).toHaveTextContent("3.117");
  });

  it("renders the what-if control", () => {
    render_();
    expect(screen.getByLabelText(/what if/i)).toBeInTheDocument();
  });
});

// US25/26: "choose an appliance". A free-text box asked the user to type a
// raw appliance_id from memory; a typo came back as a 404 that the panel
// then swallowed.
describe("Month — the what-if control", () => {
  it("offers the appliance roster as a picker, labelled with display names", () => {
    render_();
    const select = screen.getByLabelText(/what if/i);
    expect(select.tagName).toBe("SELECT");
    for (const appliance of ROSTER) {
      expect(
        screen.getByRole("option", { name: appliance.display_name }),
      ).toBeInTheDocument();
    }
  });

  it("submits the appliance ID, not the display name the user picked", async () => {
    const onWhatIf = vi.fn();
    render_({}, { onWhatIf });
    await userEvent.selectOptions(screen.getByLabelText(/what if/i), "kettle");
    await userEvent.click(screen.getByRole("button", { name: /calculate/i }));
    expect(onWhatIf).toHaveBeenCalledWith("kettle", 10);
  });

  it("submits the first roster entry when the user changes nothing", async () => {
    // The <select> shows its first option before anything is chosen, so
    // the submitted id must be that one -- not the empty initial state,
    // which would ask for an appliance the user was never shown.
    const onWhatIf = vi.fn();
    render_({}, { onWhatIf });
    await userEvent.click(screen.getByRole("button", { name: /calculate/i }));
    expect(onWhatIf).toHaveBeenCalledWith("incandescent_lamp", 10);
  });

  it("does not offer a calculation it cannot make when the roster has not loaded", async () => {
    const onWhatIf = vi.fn();
    render_({}, { onWhatIf, appliances: [] });
    const button = screen.getByRole("button", { name: /calculate/i });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(onWhatIf).not.toHaveBeenCalled();
  });
});

// The spec calls the naive-vs-true gap "the product's whole argument", and
// until now no test rendered the populated panel at all.
describe("Month — the what-if result", () => {
  it("renders no result panel before a calculation", () => {
    render_();
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
    expect(screen.queryByTestId("whatif-error")).not.toBeInTheDocument();
  });

  it("renders the naive and true savings together", () => {
    render_({}, { whatIf: WHATIF_CROSSES });
    expect(screen.getByTestId("figure-naive-saving")).toHaveTextContent(
      "RM 5.90",
    );
    expect(screen.getByTestId("figure-true-saving")).toHaveTextContent(
      "RM 11.87",
    );
  });

  it("renders the gap as a multiple, not as an absolute amount", () => {
    render_({}, { whatIf: WHATIF_CROSSES });
    const multiple = screen.getByTestId("whatif-multiple");
    expect(multiple).toHaveTextContent("2.012×");
    // "Difference" read as an absolute number of ringgit -- the one thing
    // a ratio is not. The label and value must read as one sentence.
    expect(screen.queryByText(/^Difference$/)).not.toBeInTheDocument();
    expect(screen.getByText(/true saving is/i)).toBeInTheDocument();
    expect(multiple).toHaveTextContent(/the naive/i);
    // And it must never take the ringgit accent: it is not money.
    expect(multiple).not.toHaveClass("text-ringgit");
  });

  it("warns when the saving drops back under a band boundary", () => {
    render_({}, { whatIf: WHATIF_CROSSES });
    expect(screen.getByTestId("whatif-crosses-back")).toHaveTextContent(
      /re-prices the whole month/i,
    );
  });

  it("does not warn when the saving crosses no boundary", () => {
    render_({}, { whatIf: WHATIF_NO_CROSS });
    expect(screen.queryByTestId("whatif-crosses-back")).not.toBeInTheDocument();
    // The gap runs the other way here -- the true saving is SMALLER than
    // the naive one, because the naive figure ignores the discount. The
    // panel reports it as it is.
    expect(screen.getByTestId("whatif-multiple")).toHaveTextContent("0.750×");
    expect(screen.getByTestId("figure-true-saving")).toHaveTextContent(
      "RM 1.11",
    );
  });

  it("states a failed calculation instead of silently showing nothing", () => {
    // `.catch(() => setWhatIf(null))` made the panel vanish, which is
    // indistinguishable from "not calculated yet" -- so a 404 from
    // /api/whatif looked exactly like an untouched control.
    render_({}, { whatIfError: "That what-if could not be calculated." });
    expect(screen.getByTestId("whatif-error")).toHaveTextContent(
      /could not be calculated/i,
    );
    expect(screen.queryByTestId("whatif-result")).not.toBeInTheDocument();
  });
});
