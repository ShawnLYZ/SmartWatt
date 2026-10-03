import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import {
  getActions,
  getAppliances,
  getLedger,
  getMonth,
  getPending,
  getRules,
  getSeries,
  postCancel,
  postControl,
  getWizardScatter,
  getWizardState,
  postWizardBaseline,
  postWizardCapture,
  postWizardVerify,
} from "../src/lib/api";
import { loadExample, MONTH_PAYLOAD } from "./fixtures";
import type { ApplianceRow } from "../src/lib/api";

// Ruling AF: this file was scoped ENTIRELY to Ruling AD's three
// fetch-failure paths. It is still not exhaustive App coverage, but it no
// longer scopes away from the props App hands its screens -- that gap is
// how the Appliances Ringgit column came to be priced from `cliff.marginal`
// (a difference of two whole bills) with every component test still green,
// because appliances.test.tsx supplied its own hardcoded rate and nothing
// checked what App actually passed.
//
// Ruling C (Task 5b): every export App.tsx imports from this module MUST
// be listed here -- this factory REPLACES the real module entirely, so an
// import this factory omits is `undefined` at the call site, and the
// first call throws. That is exactly what broke this file's own 8 tests
// the moment Control's fetches were added to App.tsx without a matching
// entry here.
vi.mock("../src/lib/api", () => ({
  getSeries: vi.fn(),
  getMonth: vi.fn(),
  getAppliances: vi.fn(),
  getLedger: vi.fn(),
  postWhatIf: vi.fn(),
  getPending: vi.fn(),
  getActions: vi.fn(),
  getRules: vi.fn(),
  postControl: vi.fn(),
  postCancel: vi.fn(),
  getWizardState: vi.fn(),
  getWizardScatter: vi.fn(),
  postWizardAdvance: vi.fn(),
  postWizardBaseline: vi.fn(),
  postWizardBegin: vi.fn(),
  postWizardCapture: vi.fn(),
  postWizardConfirm: vi.fn(),
  postWizardVerify: vi.fn(),
}));

// useLiveSocket() runs unconditionally on every render regardless of which
// screen is showing, and constructs a real WebSocket on mount. jsdom's
// WebSocket would otherwise attempt a genuine connection to a nonexistent
// server -- this stub just needs to exist and never error; none of these
// tests touch telemetry, so it never needs to open.
class MockSocket {
  static instances: MockSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  readyState = 0;
  close = vi.fn();
  constructor(public url: string) {
    MockSocket.instances.push(this);
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  emit(kind: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ kind, payload }) });
  }
}

beforeEach(() => {
  MockSocket.instances = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", MockSocket as unknown as typeof WebSocket);
  // Uniform reject-by-default baseline: App's own .catch() handlers
  // already tolerate every one of these failing, so a bare reject is a
  // safe default that each test only overrides where its scenario needs
  // a real payload.
  vi.mocked(getSeries).mockReset().mockRejectedValue(new Error("no series"));
  vi.mocked(getMonth).mockReset().mockRejectedValue(new Error("no month"));
  vi.mocked(getAppliances).mockReset().mockRejectedValue(new Error("no appliances"));
  vi.mocked(getLedger).mockReset().mockRejectedValue(new Error("no ledger"));
  // Same reject-by-default baseline for Control's fetches. getPending and
  // getActions are polled every 2 s (App.tsx's pollControlState effect) --
  // a 30 s `flush()` elsewhere in this file fires that ~15 times, and a
  // bare rejection swallowed by App's own .catch(() => {}) is what keeps
  // that harmless rather than flooding a test with unhandled rejections
  // or stray state updates.
  vi.mocked(getPending).mockReset().mockRejectedValue(new Error("no pending"));
  vi.mocked(getActions).mockReset().mockRejectedValue(new Error("no actions"));
  vi.mocked(getRules).mockReset().mockRejectedValue(new Error("no rules"));
  vi.mocked(postControl).mockReset().mockRejectedValue(new Error("no control"));
  vi.mocked(postCancel).mockReset().mockRejectedValue(new Error("no cancel"));
  vi.mocked(getWizardState).mockReset().mockRejectedValue(new Error("no wizard"));
  vi.mocked(getWizardScatter).mockReset().mockRejectedValue(new Error("no scatter"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Flushes fake timers AND the promise microtask queue together -- plain
 *  `vi.advanceTimersByTime` fires due timers without yielding to pending
 *  `.then()`/`.catch()` continuations in between, which is exactly what
 *  every assertion here is waiting on. */
const flush = (ms = 0) => act(() => vi.advanceTimersByTimeAsync(ms));

describe("App - Ruling AD fetch-failure paths", () => {
  it("recovers the protected mark after a later tick, once a failed appliances fetch retries", async () => {
    vi.mocked(getLedger).mockResolvedValue({
      window: "today",
      rows: [{ appliance_id: "kettle", wh: 1_000, w_mean: 500, minutes: 60 }],
    });
    const protectedKettle: ApplianceRow[] = [
      { id: "kettle", display_name: "Kettle", protected: 1, heating: 0, plug_device: null },
    ];
    vi.mocked(getAppliances)
      .mockRejectedValueOnce(new Error("startup race"))
      .mockResolvedValueOnce(protectedKettle);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Appliances" }));
    await flush();

    // First tick: getAppliances failed, so nothing is known to be
    // protected yet -- but the ledger loaded fine, so the row is there.
    expect(screen.getByTestId("row-kettle")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("row-kettle")).queryByTestId("protected-mark"),
    ).not.toBeInTheDocument();
    expect(vi.mocked(getAppliances)).toHaveBeenCalledTimes(1);

    // Second tick, 30 s later: the retry succeeds and the mark appears
    // without any user action or remount.
    await flush(30_000);
    expect(
      within(screen.getByTestId("row-kettle")).getByTestId("protected-mark"),
    ).toBeInTheDocument();
    expect(vi.mocked(getAppliances)).toHaveBeenCalledTimes(2);
  });

  it("shows a visible unavailable state for Month when getMonth fails, not a blank tab", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Month" }));
    await flush();

    expect(screen.getByTestId("month-unavailable")).toHaveTextContent(
      /not available/i,
    );
    // Confirms this is the fallback, not a Month that merely renders
    // empty-looking: the Cliff Gauge (always present given real
    // MonthPayload) must be entirely absent.
    expect(screen.queryByTestId("cliff-gauge")).not.toBeInTheDocument();
  });

  it("shows a visible unavailable state for Appliances when getLedger fails, not an empty table", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Appliances" }));
    await flush();

    expect(screen.getByTestId("appliances-unavailable")).toHaveTextContent(
      /not available/i,
    );
    // Confirms this is the fallback, not an Appliances table that
    // happens to render zero rows.
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

// Spec, Data flow: "A single reconnecting-socket hook owns connection
// state and EVERY screen reads it, so the connection indicator cannot
// disagree with itself across screens." It used to be imported by
// Live.tsx alone: if the device died three days ago, Month showed a
// confident bill, projection and Cliff Gauge with nothing saying
// ingestion had stopped.
describe("App — connection state on every screen", () => {
  it("renders exactly one connection indicator, on every screen", async () => {
    render(<App />);
    await flush();

    // Includes Control (Task 5b): the claim this test enforces is "EVERY
    // screen", not "the three that existed when this test was written" --
    // leaving a newly added screen out of the loop would let it violate
    // the very property described above with this test still green.
    for (const label of ["Live", "Month", "Appliances", "Control"]) {
      fireEvent.click(screen.getByRole("button", { name: label }));
      // getByTestId throws on multiple matches, so this asserts BOTH
      // "present on this screen" and "not duplicated by a screen that
      // still renders its own".
      expect(screen.getByTestId("connection"), label).toBeInTheDocument();
    }
  });

  it("announces a dropped socket in the header without unmounting the Live figures", async () => {
    // This assertion used to live in live.test.tsx, against a
    // ConnectionState that Live rendered itself. It moves here with the
    // indicator; the "does not unmount the figures" half is the part that
    // still belongs to a screen.
    render(<App />);
    await flush();
    const socket = MockSocket.instances[0]!;
    act(() => {
      socket.open();
      socket.emit("telemetry", loadExample("telemetry-single-load"));
    });
    expect(screen.getByTestId("figure-power")).toHaveTextContent("903 W");

    act(() => socket.onclose?.());
    expect(screen.getByTestId("connection")).toHaveAttribute(
      "data-state",
      "down",
    );
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument();
    // The last real reading is still on screen -- and still labelled by a
    // visible connection state, which is what makes that honest.
    expect(screen.getByTestId("figure-power")).toHaveTextContent("903 W");
  });

  it("keeps the indicator up on Month, where a stale bill looks most confident", async () => {
    vi.mocked(getMonth).mockResolvedValue(MONTH_PAYLOAD);
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Month" }));
    await flush();

    // The MockSocket never opens, so the hook is still "connecting" --
    // status !== "open" is the down state.
    expect(screen.getByTestId("connection")).toHaveAttribute(
      "data-state",
      "down",
    );
    // ...and it is up beside a Cliff Gauge that is rendering figures.
    expect(screen.getByTestId("cliff-gauge")).toBeInTheDocument();
  });
});

// The Critical finding's wiring, pinned where it actually broke.
describe("App — what it hands the Appliances screen", () => {
  const withLedger = () =>
    vi.mocked(getLedger).mockResolvedValue({
      window: "today",
      rows: [{ appliance_id: "kettle", wh: 120_000, w_mean: 1800, minutes: 400 }],
    });

  it("prices a row as its share of the real bill, not from cliff.marginal", async () => {
    withLedger();
    vi.mocked(getMonth).mockResolvedValue(MONTH_PAYLOAD);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Appliances" }));
    await flush();

    const cell = within(screen.getByTestId("row-kettle")).getByTestId(
      "cell-ringgit",
    );
    // 69.05 x 120/341 = 24.29912... -> RM 24.30.
    expect(cell).toHaveTextContent("RM 24.30");
    // cliff.marginal for this payload is "0.20"; 120 x 0.20 = RM 24.00 is
    // what the old wiring produced. Close enough to pass an eyeball, which
    // is exactly why it needs an assertion.
    expect(cell).not.toHaveTextContent("RM 24.00");
  });

  it("renders the RM and CO2 columns unavailable, not zero, when getMonth fails", async () => {
    // getMonth rejects by default here. With the socket healthy and
    // /api/month failing, the old `?? "0.00"` / `?? "0"` fallbacks made
    // every row read RM 0.00 and 0.00 kg -- invented figures, un-staled,
    // stated as fact.
    withLedger();

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Appliances" }));
    await flush();

    const row = within(screen.getByTestId("row-kettle"));
    expect(row.getByTestId("cell-ringgit")).toHaveAttribute(
      "data-unavailable",
      "true",
    );
    expect(row.getByTestId("cell-ringgit")).not.toHaveTextContent("RM");
    expect(row.getByTestId("cell-co2")).toHaveAttribute(
      "data-unavailable",
      "true",
    );
    expect(row.getByTestId("cell-co2")).not.toHaveTextContent("0.00");
    // The measured columns are a different source and stay real.
    expect(row.getByTestId("cell-kwh")).toHaveTextContent("120.0");
  });
});

// Task 5b: App.tsx's wiring into the Control screen. Each test below
// targets one specific piece of wiring that would otherwise be dead code
// with the suite still green -- Ruling B's device filter, the
// postControl/postCancel plumbing, and the is_demo pass-through are three
// separate things that could each individually silently break while
// control.test.tsx (which only ever sees hand-built props) stayed green.
describe("App — Control screen wiring", () => {
  const ROSTER: ApplianceRow[] = [
    { id: "desk_fan", display_name: "Desk fan", protected: 0, heating: 0, plug_device: null },
    { id: "laptop_charger", display_name: "Laptop charger", protected: 1, heating: 0, plug_device: "plug_laptop" },
    { id: "kettle", display_name: "Kettle", protected: 0, heating: 1, plug_device: "plug_kettle" },
  ];

  it("passes Control only devices that have a plug, keeping the protected one (Ruling B)", async () => {
    vi.mocked(getAppliances).mockResolvedValue(ROSTER);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Control" }));
    await flush();

    // desk_fan has plug_device: null -- a toggle for it could only ever
    // produce REFUSED_UNREGISTERED, which demonstrates missing hardware,
    // not the safety gate.
    expect(screen.queryByTestId("device-desk_fan")).not.toBeInTheDocument();
    // laptop_charger IS protected, but it HAS a plug -- it must stay, so
    // an evaluator can attempt the cut US49 asks for and watch it refused.
    expect(screen.getByTestId("device-laptop_charger")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("device-laptop_charger")).getByTestId("protected-mark"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("device-kettle")).toBeInTheDocument();
  });

  it("wires a Control command to postControl and shows the refusal that comes back", async () => {
    vi.mocked(getAppliances).mockResolvedValue(ROSTER);
    vi.mocked(postControl).mockResolvedValue({
      outcome: "REFUSED_PROTECTED",
      reason: "laptop_charger is protected and can never be cut",
    });

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Control" }));
    await flush();

    fireEvent.click(
      within(screen.getByTestId("device-laptop_charger")).getByRole("button", { name: /off/i }),
    );
    await flush();

    expect(postControl).toHaveBeenCalledWith("laptop_charger", "OFF");
    expect(screen.getByTestId("last-outcome")).toHaveTextContent("REFUSED_PROTECTED");
  });

  it("surfaces a transport failure distinctly, not as a silent no-op", async () => {
    vi.mocked(getAppliances).mockResolvedValue(ROSTER);
    vi.mocked(postControl).mockRejectedValue(new Error("network down"));

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Control" }));
    await flush();

    fireEvent.click(
      within(screen.getByTestId("device-laptop_charger")).getByRole("button", { name: /off/i }),
    );
    await flush();

    // postControl only rejects for a genuine transport/server failure --
    // never for a refusal, which arrives as an ordinary 200 (see api.ts).
    // A click that silently did nothing here would be indistinguishable
    // from a click that was never registered at all.
    expect(screen.getByTestId("last-outcome")).toHaveTextContent("REQUEST_FAILED");
  });

  it("wires the pending-cut cancel button to postCancel", async () => {
    vi.mocked(getAppliances).mockResolvedValue([]);
    vi.mocked(getPending).mockResolvedValue([
      { id: "p1", appliance_id: "incandescent_lamp", rule: "left_on", remaining_s: 42 },
    ]);
    vi.mocked(postCancel).mockResolvedValue({ cancelled: true });

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Control" }));
    await flush();

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    await flush();

    expect(postCancel).toHaveBeenCalledWith("p1");
  });

  it("passes the is_demo flag from /api/rules through to the thresholds panel (Ruling A)", async () => {
    vi.mocked(getAppliances).mockResolvedValue([]);
    vi.mocked(getRules).mockResolvedValue({
      left_on_seconds: 30,
      standby_band_w: [1, 12] as [number, number],
      standby_seconds: 120,
      grace_seconds: 60,
      is_demo: true,
      left_on_targets: ["incandescent_lamp"],
    });

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Control" }));
    await flush();

    expect(screen.getByTestId("rules-panel")).toHaveAttribute("data-demo", "true");
  });

  it("renders no thresholds panel when /api/rules is unavailable, rather than guessing", async () => {
    // getRules rejects by default via this file's beforeEach.
    vi.mocked(getAppliances).mockResolvedValue([]);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Control" }));
    await flush();

    expect(screen.queryByTestId("rules-panel")).not.toBeInTheDocument();
  });

  it("keeps polling pending/actions roughly every 2 s, not just once at mount", async () => {
    vi.mocked(getAppliances).mockResolvedValue([]);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Control" }));
    await flush();
    const callsBefore = vi.mocked(getPending).mock.calls.length;

    // getPending/getActions reject every time in this test (beforeEach's
    // default) -- proving the interval keeps firing across a 30 s advance
    // is what stops the setInterval() call itself from being dead code
    // that only the initial, mount-time call made look wired up.
    await flush(30_000);

    expect(vi.mocked(getPending).mock.calls.length).toBeGreaterThan(callsBefore + 5);
    expect(screen.getByTestId("connection")).toBeInTheDocument();
  });
});

describe("App — Setup wiring (S8 final review)", () => {
  const WIZARD = {
    step: "quiet",
    confirmed: 0,
    required: 20,
    awaiting_confirmation: 0,
    streak: 0,
    streak_required: 3,
    baseline_w: 4.2,
    fingerprints_path: "firmware/data/fingerprints.csv",
    classes: ["apple_charger", "phone_charger"],
  };

  const openSetup = async (state: Record<string, unknown> = WIZARD) => {
    vi.mocked(getWizardState).mockResolvedValue(state as never);
    vi.mocked(getWizardScatter).mockResolvedValue({ points: [], labels: [] });
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Setup" }));
    await flush();
  };

  it("sends only operator-known conditions on a capture, never measured ones (R24)", async () => {
    vi.mocked(postWizardCapture).mockReset().mockResolvedValue({
      accepted: false, reason: "no edge detected", training_id: null,
      captured: 0, required: 20,
    });
    await openSetup();
    // Live telemetry HAS arrived: the old code copied its vrms/freq, and
    // fell back to zeros without it. Neither may reach the request now.
    const socket = MockSocket.instances[0]!;
    act(() => {
      socket.open();
      socket.emit("telemetry", loadExample("telemetry-single-load"));
    });
    const row = screen.getByTestId("capture-row-apple_charger");
    fireEvent.click(within(row).getByRole("button", { name: "Capture ON" }));
    await flush();

    const [label, edge, conditions] = vi.mocked(postWizardCapture).mock.calls[0]!;
    expect([label, edge]).toEqual(["apple_charger", "on"]);
    expect(Object.keys(conditions).sort()).toEqual(["session_id"]);
    expect(conditions).not.toHaveProperty("background_w");
    expect(conditions).not.toHaveProperty("vrms_mean");
    expect(conditions).not.toHaveProperty("freq_mean");
  });

  it("shows a verify 409 visibly instead of swallowing it (R27a)", async () => {
    vi.mocked(postWizardVerify).mockReset().mockRejectedValue(
      Object.assign(new Error("409"), {
        detail: "no event has arrived since the last verification",
      }),
    );
    await openSetup({ ...WIZARD, step: "verify" });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await flush();
    expect(screen.getByTestId("verify-refused")).toHaveTextContent(
      "no event has arrived since the last verification",
    );
  });

  it("shows the device's verify answer (R27a)", async () => {
    vi.mocked(postWizardVerify).mockReset().mockResolvedValue({
      expected: "apple_charger", actual: "phone_charger", correct: false,
      streak: 0, required: 3, passed: false,
    });
    await openSetup({ ...WIZARD, step: "verify" });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await flush();
    expect(screen.getByTestId("verify-result")).toHaveTextContent(
      "device answered phone_charger",
    );
  });

  it("records the baseline through the server and shows a refusal (R24)", async () => {
    vi.mocked(postWizardBaseline).mockReset().mockRejectedValue(
      Object.assign(new Error("409"), { detail: "no telemetry has been stored yet" }),
    );
    await openSetup({ ...WIZARD, step: "baseline", baseline_w: null });
    fireEvent.click(screen.getByRole("button", { name: /record baseline/i }));
    await flush();
    expect(postWizardBaseline).toHaveBeenCalledWith();
    expect(screen.getByTestId("baseline-refused")).toHaveTextContent(
      "no telemetry has been stored yet",
    );
  });
});
