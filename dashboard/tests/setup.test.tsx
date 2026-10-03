import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Setup } from "../src/screens/Setup/Setup";

const STATE = {
  step: "quiet",
  confirmed: 12,
  required: 50,
  awaiting_confirmation: 0,
  streak: 0,
  streak_required: 3,
  baseline_w: 4.2,
  fingerprints_path: "C:/SmartWatt/fingerprints.csv",
  classes: ["kettle", "desk_fan", "incandescent_lamp", "led_bulb", "laptop_charger"],
};

const SCATTER = {
  points: [
    { x: -1.2, y: 0.4, label: "kettle" },
    { x: 1.1, y: -0.3, label: "desk_fan" },
    { x: 0.9, y: 0.8, label: "incandescent_lamp" },
  ],
  labels: ["kettle", "desk_fan", "incandescent_lamp"],
};

const render_ = (overrides = {}) =>
  render(
    <Setup
      state={STATE}
      scatter={SCATTER}
      onBegin={vi.fn()}
      onCapture={vi.fn()}
      onConfirm={vi.fn()}
      onAdvance={vi.fn()}
      onVerify={vi.fn()}
      lastCapture={null}
      {...overrides}
    />,
  );

describe("Setup", () => {
  it("shows the current step", () => {
    render_();
    expect(screen.getByTestId("wizard-step")).toHaveTextContent(/quiet/i);
  });

  it("shows capture progress", () => {
    render_();
    expect(screen.getByTestId("wizard-progress")).toHaveTextContent("12");
    expect(screen.getByTestId("wizard-progress")).toHaveTextContent("50");
  });

  it("shows the recorded baseline", () => {
    render_();
    expect(screen.getByTestId("baseline")).toHaveTextContent("4.2");
  });

  it("renders a visible confirmation prompt after a capture", () => {
    render_({
      lastCapture: { accepted: true, training_id: "kettle-on-abc123", reason: null },
    });
    expect(screen.getByTestId("confirm-prompt")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();
  });

  it("renders a failed capture differently from a successful one", () => {
    render_({
      lastCapture: { accepted: false, training_id: null, reason: "no edge detected" },
    });
    expect(screen.getByTestId("capture-failed")).toHaveTextContent("no edge detected");
    expect(screen.queryByTestId("confirm-prompt")).not.toBeInTheDocument();
  });

  it("prompts for a concurrent appliance in the overlapped step", () => {
    render_({ state: { ...STATE, step: "overlapped" } });
    expect(screen.getByText(/already running/i)).toBeInTheDocument();
  });

  it("shows the verification streak", () => {
    render_({ state: { ...STATE, step: "verify", streak: 2 } });
    expect(screen.getByTestId("verify-streak")).toHaveTextContent("2");
    expect(screen.getByTestId("verify-streak")).toHaveTextContent("3");
  });

  it("says setup is not complete until verification passes", () => {
    render_({ state: { ...STATE, step: "verify", streak: 1 } });
    expect(screen.getByText(/not complete/i)).toBeInTheDocument();
  });

  it("renders the signature scatter", () => {
    render_();
    expect(screen.getByTestId("signature-scatter")).toBeInTheDocument();
    expect(screen.getAllByTestId(/^scatter-point-/)).toHaveLength(3);
  });

  it("legends every class in the scatter", () => {
    render_();
    for (const label of SCATTER.labels) {
      expect(screen.getByTestId(`scatter-legend-${label}`)).toBeInTheDocument();
    }
  });

  it("shows the on-disk fingerprints path", () => {
    // US58: the plain editable file must be discoverable, not buried.
    render_();
    expect(screen.getByTestId("fingerprints-path")).toHaveTextContent(
      "fingerprints.csv",
    );
  });

  it("calls back on advance", async () => {
    const onAdvance = vi.fn();
    render_({ onAdvance });
    await userEvent.click(screen.getByRole("button", { name: /next step/i }));
    expect(onAdvance).toHaveBeenCalled();
  });

  // Fix round 1, Important finding 1: the push panel must read from
  // `state.push` (persisted server-side by `wizard.py`'s R21 fix), not
  // from advance-response-only keys that vanish on the next poll.
  it("shows a visible push failure when state.push.published is false", () => {
    render_({
      state: {
        ...STATE,
        step: "push",
        push: { published: false, fingerprint_id: null, reason: "no MQTT client" },
      },
    });
    expect(screen.getByTestId("push-result")).toHaveTextContent("no MQTT client");
    expect(screen.getByTestId("push-result")).not.toHaveTextContent("Published.");
  });

  it("shows the fingerprint_id when state.push.published is true", () => {
    render_({
      state: {
        ...STATE,
        step: "push",
        push: { published: true, fingerprint_id: "abcd1234", reason: null },
      },
    });
    expect(screen.getByTestId("push-result")).toHaveTextContent("abcd1234");
  });

  // Minor finding, fix round 1: a 409 advance refusal shown visibly.
  it("shows a visible advance refusal", () => {
    render_({ advanceError: "Could not advance: quotas not met." });
    expect(screen.getByTestId("advance-refused")).toHaveTextContent(
      "quotas not met",
    );
  });

  // -- R24: the baseline is recorded by the server, from telemetry --------
  describe("baseline step", () => {
    const BASELINE = { ...STATE, step: "baseline", baseline_w: null };

    it("offers a Record baseline action", async () => {
      const onRecordBaseline = vi.fn();
      render_({ state: BASELINE, onRecordBaseline });
      await userEvent.click(screen.getByRole("button", { name: /record baseline/i }));
      expect(onRecordBaseline).toHaveBeenCalled();
    });

    it("blocks Next until a baseline is recorded", () => {
      render_({ state: BASELINE });
      expect(screen.getByRole("button", { name: /next step/i })).toBeDisabled();
      expect(screen.getByTestId("baseline")).toHaveTextContent(/not recorded/i);
    });

    it("allows Next once a baseline is recorded, and shows it", () => {
      render_({ state: { ...BASELINE, baseline_w: 4.2 } });
      expect(screen.getByRole("button", { name: /next step/i })).toBeEnabled();
      expect(screen.getByTestId("baseline")).toHaveTextContent("4.2 W");
    });

    it("shows why a baseline could not be recorded", () => {
      render_({ state: BASELINE, baselineError: "no telemetry has been stored yet" });
      expect(screen.getByTestId("baseline-refused")).toHaveTextContent(
        "no telemetry",
      );
    });
  });

  // -- R25: the prompt describes the bound event --------------------------
  it("shows the bound event's delta P, state and time in the confirm prompt", () => {
    render_({
      lastCapture: {
        accepted: true, training_id: "kettle-on-001", reason: null,
        captured: 0, required: 50,
        event_ts: 1754035188.412, delta_p: 848.2, event_reason: null,
      },
    });
    const prompt = screen.getByTestId("confirm-prompt");
    expect(prompt).toHaveTextContent("848.2 W");
    expect(prompt).toHaveTextContent(/clean edge/i);
    // 1754035188.412 is 07:59:48 UTC, 15:59:48 in Kuching (UTC+8).
    expect(prompt).toHaveTextContent("15:59:48");
  });

  it("names a rejected-but-settled edge's state in the confirm prompt", () => {
    render_({
      lastCapture: {
        accepted: true, training_id: "kettle-on-001", reason: null,
        captured: 0, required: 50,
        event_ts: 1754035188.412, delta_p: -12.5, event_reason: "distance_threshold",
      },
    });
    const prompt = screen.getByTestId("confirm-prompt");
    expect(prompt).toHaveTextContent("-12.5 W");
    expect(prompt).toHaveTextContent("distance_threshold");
  });

  it("describes the refused event when a capture fails on it", () => {
    render_({
      lastCapture: {
        accepted: false, training_id: null,
        reason: "the bound on edge overlapped another edge (overlapping_edges)",
        captured: 0, required: 50,
        event_ts: 1754035188.412, delta_p: 300, event_reason: "overlapping_edges",
      },
    });
    const failed = screen.getByTestId("capture-failed");
    expect(failed).toHaveTextContent("overlapping_edges");
    expect(failed).toHaveTextContent("300 W");
  });

  // -- R26 ------------------------------------------------------------------
  it("hides the confirm prompt outside the capture steps", () => {
    render_({
      state: { ...STATE, step: "push" },
      lastCapture: { accepted: true, training_id: "kettle-on-001", reason: null },
    });
    expect(screen.queryByTestId("confirm-prompt")).not.toBeInTheDocument();
  });

  // -- R22: the device's id is the gate ---------------------------------------
  it("shows device-reported and expected ids, with the uploadfs hint until they match", () => {
    render_({
      state: {
        ...STATE, step: "push",
        push: { published: true, fingerprint_id: "abcd1234", reason: null },
        expected_fingerprint_id: "abcd1234",
        device_fingerprint_id: "0badc0de",
      },
    });
    const ids = screen.getByTestId("fingerprint-ids");
    expect(ids).toHaveTextContent("device reports 0badc0de, expected abcd1234");
    expect(screen.getByTestId("uploadfs-hint")).toHaveTextContent(
      "pio run -e esp32-s3 -t uploadfs",
    );
    expect(screen.getByTestId("uploadfs-hint")).toHaveTextContent(/no fingerprint subscriber/i);
  });

  it("says the device reports nothing when it has no id", () => {
    render_({
      state: {
        ...STATE, step: "push",
        push: { published: false, fingerprint_id: null, reason: null },
        expected_fingerprint_id: "abcd1234",
        device_fingerprint_id: null,
      },
    });
    expect(screen.getByTestId("fingerprint-ids")).toHaveTextContent(
      "device reports nothing yet, expected abcd1234",
    );
    expect(screen.getByTestId("uploadfs-hint")).toBeInTheDocument();
  });

  it("drops the uploadfs hint once the ids match", () => {
    render_({
      state: {
        ...STATE, step: "push",
        push: { published: true, fingerprint_id: "abcd1234", reason: null },
        expected_fingerprint_id: "abcd1234",
        device_fingerprint_id: "abcd1234",
      },
    });
    expect(screen.getByTestId("fingerprint-ids")).toHaveTextContent(
      "device reports abcd1234, expected abcd1234",
    );
    expect(screen.queryByTestId("uploadfs-hint")).not.toBeInTheDocument();
  });

  it("keeps the ids visible in the verify step", () => {
    render_({
      state: {
        ...STATE, step: "verify",
        expected_fingerprint_id: "abcd1234",
        device_fingerprint_id: "abcd1234",
      },
    });
    expect(screen.getByTestId("fingerprint-ids")).toHaveTextContent("abcd1234");
  });

  // -- R27a: verify shows the device's answer, and a 409 ----------------------
  it("shows what the device answered on a verify", () => {
    render_({
      state: { ...STATE, step: "verify", streak: 0 },
      lastVerify: {
        expected: "kettle", actual: "desk_fan", correct: false,
        streak: 0, required: 3, passed: false,
      },
    });
    const result = screen.getByTestId("verify-result");
    expect(result).toHaveTextContent("expected kettle");
    expect(result).toHaveTextContent("device answered desk_fan");
    expect(result).toHaveTextContent(/wrong/i);
  });

  it("shows a verify refusal instead of swallowing it", () => {
    render_({
      state: { ...STATE, step: "verify" },
      verifyError: "no event has arrived since the last verification",
    });
    expect(screen.getByTestId("verify-refused")).toHaveTextContent("no event has arrived");
  });

  // -- R27c: a broken training file is stated -------------------------------
  it("shows a training-file error", () => {
    render_({
      state: { ...STATE, file_error: "fingerprints.csv cannot be read: line 7: pf_disp is empty" },
    });
    expect(screen.getByTestId("file-error")).toHaveTextContent("line 7");
  });

  it("shows a scatter error", () => {
    render_({
      scatter: { points: [], labels: [], error: "the training file cannot be read: line 7" },
    });
    expect(screen.getByTestId("scatter-error")).toHaveTextContent("line 7");
  });
});
