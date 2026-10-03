import { act, renderHook } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLiveSocket } from "../src/hooks/useLiveSocket";
import { loadExample } from "./fixtures";

class MockSocket {
  static instances: MockSocket[] = [];
  /**
   * A real WebSocket's close handshake is ASYNCHRONOUS: `close()` returns
   * at once and `onclose` fires later. The synchronous default keeps every
   * other test here simple; the StrictMode test flips this on, because the
   * orphaned-socket bug it pins exists only in the asynchronous ordering.
   */
  static asyncClose = false;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  readyState = 0;
  close = vi.fn(() => {
    const fire = () => {
      this.readyState = 3;
      this.onclose?.();
    };
    if (MockSocket.asyncClose) setTimeout(fire, 0);
    else fire();
  });
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
  MockSocket.asyncClose = false;
  vi.stubGlobal("WebSocket", MockSocket as unknown as typeof WebSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useLiveSocket", () => {
  it("starts connecting", () => {
    const { result } = renderHook(() => useLiveSocket());
    expect(result.current.status).toBe("connecting");
  });

  it("goes open once the socket opens", () => {
    const { result } = renderHook(() => useLiveSocket());
    act(() => MockSocket.instances[0]!.open());
    expect(result.current.status).toBe("open");
  });

  it("stores the latest telemetry", () => {
    const { result } = renderHook(() => useLiveSocket());
    act(() => {
      const socket = MockSocket.instances[0]!;
      socket.open();
      socket.emit("telemetry", loadExample("telemetry-single-load"));
    });
    expect(result.current.telemetry?.electrical.p).toBeCloseTo(902.5);
  });

  it("stores the latest event", () => {
    const { result } = renderHook(() => useLiveSocket());
    act(() => {
      const socket = MockSocket.instances[0]!;
      socket.open();
      socket.emit("event", loadExample("event-on-confident"));
    });
    expect(result.current.lastEvent?.label).toBe("kettle");
  });

  it("reconnects after a drop without a page reload", async () => {
    renderHook(() => useLiveSocket());
    act(() => MockSocket.instances[0]!.open());
    act(() => MockSocket.instances[0]!.onclose?.());
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(MockSocket.instances.length).toBeGreaterThan(1);
  });

  it("reports reconnecting while down", () => {
    const { result } = renderHook(() => useLiveSocket());
    act(() => MockSocket.instances[0]!.open());
    act(() => MockSocket.instances[0]!.onclose?.());
    expect(result.current.status).toBe("reconnecting");
  });

  it("stales the figures after five seconds of silence", () => {
    const { result } = renderHook(() => useLiveSocket());
    act(() => {
      const socket = MockSocket.instances[0]!;
      socket.open();
      socket.emit("telemetry", loadExample("telemetry-single-load"));
    });
    expect(result.current.stale).toBe(false);
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(result.current.stale).toBe(true);
  });

  it("clears stale when telemetry resumes", () => {
    const { result } = renderHook(() => useLiveSocket());
    const socket = () => MockSocket.instances[0]!;
    act(() => {
      socket().open();
      socket().emit("telemetry", loadExample("telemetry-single-load"));
    });
    act(() => vi.advanceTimersByTime(6000));
    expect(result.current.stale).toBe(true);
    act(() => socket().emit("telemetry", loadExample("telemetry-single-load")));
    expect(result.current.stale).toBe(false);
  });

  it("ignores a syntax-broken frame instead of throwing, and does not treat it as a heartbeat", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useLiveSocket());
    const socket = () => MockSocket.instances[0]!;
    act(() => {
      socket().open();
      socket().emit("telemetry", loadExample("telemetry-single-load"));
    });
    expect(result.current.telemetry?.electrical.p).toBeCloseTo(902.5);

    // Real telemetry has stopped arriving; a malformed frame shows up
    // partway through the staleness window. It must not refresh the
    // clock, or a dead stream that emits garbage would read as live
    // forever instead of going stale.
    act(() => vi.advanceTimersByTime(4000));
    act(() => {
      expect(() => socket().onmessage?.({ data: "{" })).not.toThrow();
    });
    expect(result.current.telemetry?.electrical.p).toBeCloseTo(902.5);
    expect(warn).toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(2000));
    expect(result.current.stale).toBe(true);

    warn.mockRestore();
  });

  it("ignores a frame that parses to null instead of throwing, and does not treat it as a heartbeat", () => {
    // JSON.parse("null") succeeds -- this is not a syntax error, so the
    // parse-only guard from the first fix round let it straight through
    // to `.kind`, throwing uncaught. The shape check inside the same try
    // is what closes this.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useLiveSocket());
    const socket = () => MockSocket.instances[0]!;
    act(() => {
      socket().open();
      socket().emit("telemetry", loadExample("telemetry-single-load"));
    });
    expect(result.current.telemetry?.electrical.p).toBeCloseTo(902.5);

    act(() => vi.advanceTimersByTime(4000));
    act(() => {
      expect(() => socket().onmessage?.({ data: "null" })).not.toThrow();
    });
    expect(result.current.telemetry?.electrical.p).toBeCloseTo(902.5);
    expect(warn).toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(2000));
    expect(result.current.stale).toBe(true);

    warn.mockRestore();
  });

  it("ignores valid JSON of the wrong shape instead of throwing, and does not treat it as a heartbeat", () => {
    // An array is valid JSON and a genuine object, but has no `kind`
    // string -- covers the shape check's third branch, not just the
    // null case.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useLiveSocket());
    const socket = () => MockSocket.instances[0]!;
    act(() => {
      socket().open();
      socket().emit("telemetry", loadExample("telemetry-single-load"));
    });
    expect(result.current.telemetry?.electrical.p).toBeCloseTo(902.5);

    act(() => vi.advanceTimersByTime(4000));
    act(() => {
      expect(() => socket().onmessage?.({ data: "[1,2,3]" })).not.toThrow();
    });
    expect(result.current.telemetry?.electrical.p).toBeCloseTo(902.5);
    expect(warn).toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(2000));
    expect(result.current.stale).toBe(true);

    warn.mockRestore();
  });

  it("connects to a same-origin relative path", () => {
    renderHook(() => useLiveSocket());
    expect(MockSocket.instances[0]!.url).toContain("/api/ws");
    expect(MockSocket.instances[0]!.url).not.toContain(".local");
  });

  it("does not orphan the first socket when StrictMode remounts the effect", async () => {
    // React 19 StrictMode runs the effect, tears it down, and runs it
    // again. With the disposal flag held in a useRef -- SHARED across
    // effect instances -- that sequence was: effect1 connects; cleanup1
    // sets closed=true and calls close(); effect2 sets closed=false;
    // socket1's deferred onclose then fires, reads `false`, flashes
    // "Reconnecting..." and schedules a retry that no cleanup owns.
    //
    // A `let` local to the effect is captured per instance, so socket1's
    // handler can only ever see its own disposal flag.
    MockSocket.asyncClose = true;
    const { result } = renderHook(() => useLiveSocket(), {
      wrapper: StrictMode,
    });

    // Sanity gate: if StrictMode were not double-invoking the effect,
    // this test would pass for a reason that has nothing to do with the
    // fix, so the precondition is asserted rather than assumed.
    expect(MockSocket.instances.length).toBe(2);
    const created = MockSocket.instances.length;

    // Socket 1's close handshake completes now, with effect 2 already live.
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.status).toBe("connecting");

    // ...and no retry was scheduled by the orphan, however long we wait.
    await act(async () => {
      vi.advanceTimersByTime(20_000);
    });
    expect(MockSocket.instances.length).toBe(created);
    expect(result.current.status).toBe("connecting");
  });
});
