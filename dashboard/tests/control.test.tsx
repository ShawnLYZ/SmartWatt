import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Control } from "../src/screens/Control/Control";

const DEVICES = [
  { appliance_id: "incandescent_lamp", display_name: "Incandescent lamp", plug_device: "plug_lamp", protected: false, heating: false },
  { appliance_id: "kettle", display_name: "Kettle", plug_device: "plug_kettle", protected: false, heating: true },
  { appliance_id: "laptop_charger", display_name: "Laptop charger", plug_device: "plug_laptop", protected: true, heating: false },
];

// `device` carries the APPLIANCE id on every row, refusals included --
// what the server actually writes (gate.py's _log), not a plug name. A
// fixture still saying `plug_lamp` here would be transcribing the shape
// the log used to have.
const ACTIONS = [
  { ts: 1754035200, actor: "rule", device: "incandescent_lamp", command: "OFF", rule: "left_on", outcome: "CONFIRMED", reason: null },
  { ts: 1754035100, actor: "user", device: "laptop_charger", command: "OFF", rule: null, outcome: "REFUSED_PROTECTED", reason: "laptop_charger is protected and can never be cut" },
];

// rules.py's actual RuleConfig constants (SHIPPED_DEFAULTS / DEMO_DEFAULTS),
// not invented round numbers -- a fixture that drifts from the real config
// is exactly the trap "verify by running, don't transcribe" exists for.
const SHIPPED_RULES = {
  left_on_seconds: 4 * 3600,
  standby_band_w: [1, 12] as [number, number],
  standby_seconds: 6 * 3600,
  grace_seconds: 60,
  is_demo: false,
  left_on_targets: ["incandescent_lamp"],
};

const DEMO_RULES = {
  left_on_seconds: 30,
  standby_band_w: [1, 12] as [number, number],
  standby_seconds: 120,
  grace_seconds: 60,
  is_demo: true,
  left_on_targets: ["incandescent_lamp"],
};

const render_ = (overrides = {}) =>
  render(
    <Control
      devices={DEVICES}
      pending={[]}
      actions={ACTIONS}
      onCommand={vi.fn()}
      onCancel={vi.fn()}
      lastOutcome={null}
      rules={null}
      {...overrides}
    />,
  );

describe("Control", () => {
  it("renders a toggle per registered plug", () => {
    render_();
    expect(screen.getAllByRole("button", { name: /off/i })).toHaveLength(3);
  });

  it("marks the protected device un-cuttable", () => {
    render_();
    expect(
      within(screen.getByTestId("device-laptop_charger")).getByTestId("protected-mark"),
    ).toBeInTheDocument();
  });

  it("leaves the protected toggle ACTIVE, not disabled", () => {
    // US49 asks an evaluator to ATTEMPT a protected cut and watch the
    // system refuse. A greyed-out button proves nothing.
    render_();
    const button = within(screen.getByTestId("device-laptop_charger"))
      .getByRole("button", { name: /off/i });
    expect(button).toBeEnabled();
  });

  it("issues the command when the protected toggle is clicked", async () => {
    const onCommand = vi.fn();
    render_({ onCommand });
    await userEvent.click(
      within(screen.getByTestId("device-laptop_charger")).getByRole("button", { name: /off/i }),
    );
    expect(onCommand).toHaveBeenCalledWith("laptop_charger", "OFF");
  });

  it("renders the refusal when one comes back", async () => {
    render_({
      lastOutcome: {
        outcome: "REFUSED_PROTECTED",
        reason: "laptop_charger is protected and can never be cut",
      },
    });
    expect(screen.getByTestId("last-outcome")).toHaveTextContent(/protected/i);
  });

  it("marks heating devices one-way", () => {
    render_();
    expect(
      within(screen.getByTestId("device-kettle")).getByTestId("heating-mark"),
    ).toBeInTheDocument();
  });

  it("renders the pending-cut countdown", () => {
    render_({
      pending: [{ id: "p1", appliance_id: "incandescent_lamp", rule: "left_on", remaining_s: 42 }],
    });
    expect(screen.getByTestId("pending-p1")).toHaveTextContent("42");
    expect(screen.getByTestId("pending-p1")).toHaveTextContent("left_on");
  });

  it("offers a cancel button on every pending cut", async () => {
    const onCancel = vi.fn();
    render_({
      pending: [{ id: "p1", appliance_id: "incandescent_lamp", rule: "left_on", remaining_s: 42 }],
      onCancel,
    });
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledWith("p1");
  });

  it("renders no pending panel when nothing is pending", () => {
    render_();
    expect(screen.queryByTestId(/^pending-/)).not.toBeInTheDocument();
  });

  it("renders the action log", () => {
    render_();
    expect(screen.getAllByTestId(/^action-/)).toHaveLength(2);
  });

  it("renders refusals in the log, visually distinct", () => {
    render_();
    const refusal = screen.getByTestId("action-1");
    expect(refusal).toHaveAttribute("data-refused", "true");
    expect(refusal).toHaveTextContent("REFUSED_PROTECTED");
  });

  it("shows the refusal reason in the log", () => {
    render_();
    expect(screen.getByTestId("action-1")).toHaveTextContent(/can never be cut/i);
  });

  it("labels every column of the action log", () => {
    // Important 3: the identifier column is read by an evaluator who did
    // not write the system. Asserting the LABELS, not just that a header
    // exists -- an unnamed column is what made a mixed namespace
    // invisible in the first place.
    render_();
    for (const label of ["Actor", "Appliance", "Command", "Rule", "Outcome", "Reason"]) {
      expect(screen.getByRole("columnheader", { name: label })).toBeInTheDocument();
    }
  });

  it("names the appliance, not the plug, on a rule-sent row", () => {
    // Both rows in the log speak one namespace. A row naming `plug_lamp`
    // could not be lined up against the refusal row below it, which has
    // only ever carried an appliance id.
    render_();
    expect(screen.getByTestId("action-0")).toHaveTextContent("incandescent_lamp");
    expect(screen.getByTestId("action-0")).not.toHaveTextContent("plug_lamp");
  });

  // Ruling A: /api/rules exists specifically to carry `is_demo`, so a
  // demonstration threshold is never mistaken on screen for the shipped
  // default. The shipped/demo pair below each assert the OTHER marker is
  // ABSENT, not just that their own is present -- neither test would still
  // pass if Control stopped distinguishing the two states altogether (e.g.
  // if it always rendered one badge, or neither).
  describe("rules panel (Ruling A: demo vs. shipped thresholds)", () => {
    it("renders no rules panel when the active thresholds are unavailable", () => {
      render_({ rules: null });
      expect(screen.queryByTestId("rules-panel")).not.toBeInTheDocument();
    });

    it("shows the shipped thresholds with a positive shipped marker, no demo badge", () => {
      render_({ rules: SHIPPED_RULES });
      const panel = screen.getByTestId("rules-panel");
      expect(panel).toHaveAttribute("data-demo", "false");
      expect(within(panel).getByTestId("shipped-badge")).toBeInTheDocument();
      expect(within(panel).queryByTestId("demo-badge")).not.toBeInTheDocument();
      // Not just the flag -- the actual figures, so nothing is hidden.
      expect(panel).toHaveTextContent("14400");
      expect(panel).toHaveTextContent("21600");
    });

    it("marks demo thresholds unmistakably, with the demo figures still visible", () => {
      render_({ rules: DEMO_RULES });
      const panel = screen.getByTestId("rules-panel");
      expect(panel).toHaveAttribute("data-demo", "true");
      expect(within(panel).getByTestId("demo-badge")).toHaveTextContent(/demo/i);
      expect(within(panel).queryByTestId("shipped-badge")).not.toBeInTheDocument();
      // The 30 s demo threshold itself must still be legible next to the
      // warning -- a badge with no figures would hide exactly the number
      // an evaluator needs to not be misled by.
      expect(panel).toHaveTextContent("30");
      expect(panel).toHaveTextContent("120");
    });

    it("names the rule the left_on threshold actually targets", () => {
      render_({ rules: DEMO_RULES });
      expect(screen.getByTestId("rules-panel")).toHaveTextContent("incandescent_lamp");
    });
  });
});
