import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  postWizardBaseline,
  postWizardCapture,
  postWizardVerify,
} from "../src/lib/api";

/**
 * Fix round 1, Important finding 2: R8/R14 (the implementer contract's own
 * rulings) say the wizard's `capture`/`verify` requests must OMIT `event`
 * and `actual` respectively, so the server always binds to its own newest
 * device event/answer rather than trusting whatever the client sends. That
 * contract lived only in `lib/api.ts`'s two POST bodies with nothing
 * asserting it -- this file is the assertion: it mocks `fetch` and reads
 * the exact JSON body each function sent.
 */
describe("wizard API request shapes (R8/R14)", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({}),
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("postWizardCapture sends no event property", async () => {
    await postWizardCapture("kettle", "on", { notes: "capture 1" });

    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/wizard/capture");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).not.toHaveProperty("event");
    expect(body).toEqual({
      label: "kettle",
      edge: "on",
      conditions: { notes: "capture 1" },
    });
  });

  it("postWizardVerify sends no actual property", async () => {
    await postWizardVerify("kettle");

    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/wizard/verify");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).not.toHaveProperty("actual");
    expect(body).toEqual({ expected: "kettle" });
  });
});

describe("wizard refusals and the baseline request (S8 final review)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("postWizardBaseline POSTs to /api/wizard/baseline with no figure in it (R24)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }),
    );
    await postWizardBaseline();
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/wizard/baseline");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({});
  });

  it("a refused POST carries the server's detail, so it can be shown (R27a)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({ detail: "no event has arrived since the last verification" }),
      }),
    );
    const error = await postWizardVerify("kettle").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).detail).toBe(
      "no event has arrived since the last verification",
    );
    expect((error as ApiError).message).toContain("no event has arrived");
  });

  it("a refused POST without a JSON body still says which request and status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => {
          throw new SyntaxError("not json");
        },
      }),
    );
    const error = await postWizardVerify("kettle").catch((e: unknown) => e);
    expect((error as ApiError).detail).toBeNull();
    expect((error as ApiError).message).toBe("/api/wizard/verify: 500");
  });
});
