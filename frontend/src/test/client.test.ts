/** Client shape tests against a mocked global fetch. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, apiErrorMessage } from "../api/client";

function fetchMock(res: { status?: number; body?: unknown; text?: string }) {
  return vi.fn().mockResolvedValue(
    res.text !== undefined
      ? new Response(res.text, { status: res.status ?? 200 })
      : Response.json(res.body ?? {}, { status: res.status ?? 200 })
  );
}

let origFetch: typeof fetch;

beforeEach(() => { origFetch = globalThis.fetch; });
afterEach(() => { globalThis.fetch = origFetch; vi.restoreAllMocks(); });

describe("api client", () => {
  it("GETs health at /api base", async () => {
    const f = fetchMock({ body: { status: "ok" } });
    globalThis.fetch = f as unknown as typeof fetch;
    expect(await api.health()).toEqual({ status: "ok" });
    expect(f.mock.calls[0][0]).toBe("/api/health");
  });

  it("POSTs job commands with JSON body", async () => {
    const f = fetchMock({ body: { accepted: true } });
    globalThis.fetch = f as unknown as typeof fetch;
    await api.move(10, 20, "mm");
    const [url, init] = f.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/device/move");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ x: 10, y: 20, units: "mm" });
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("uploads SVG via FormData", async () => {
    const f = fetchMock({ body: { id: "f1", name: "a.svg", size: 3, sanitize: { removed: [] } } });
    globalThis.fetch = f as unknown as typeof fetch;
    const res = await api.uploadSvg(new File(["<svg/>"], "a.svg", { type: "image/svg+xml" }));
    expect(res.id).toBe("f1");
    const init = f.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
  });

  it("throws ApiError with FastAPI detail on 422", async () => {
    globalThis.fetch = fetchMock({ status: 422, body: { detail: "pen must be 1..6" } }) as unknown as typeof fetch;
    const err = await api.createJob({ file_id: "x", paper: "a4", pen_map: { a: 9 }, options: {} }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(422);
    expect(apiErrorMessage(err)).toBe("pen must be 1..6");
  });

  it("handles object detail (hpgl validation)", async () => {
    globalThis.fetch = fetchMock({ status: 422, body: { detail: { message: "HP-GL rejected" } } }) as unknown as typeof fetch;
    const err = await api.uploadHpgl(new File(["PU;"], "a.hpgl")).catch((e) => e);
    expect(apiErrorMessage(err)).toContain("HP-GL rejected");
  });

  it("network failure → ApiError status 0 with retry hint", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError("fetch failed")) as unknown as typeof fetch;
    const err = await api.listJobs().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
    expect(apiErrorMessage(err)).toMatch(/Backend unreachable/);
  });

  it("jobPreview returns SVG text", async () => {
    globalThis.fetch = fetchMock({ text: "<svg xmlns='x'></svg>" }) as unknown as typeof fetch;
    expect(await api.jobPreview("j1")).toContain("<svg");
  });

  it("jobPreview 404 propagates", async () => {
    globalThis.fetch = fetchMock({ status: 404, body: { detail: "preview not ready" } }) as unknown as typeof fetch;
    const err = await api.jobPreview("j1").catch((e) => e);
    expect((err as ApiError).status).toBe(404);
  });

  it("PUT settings sends custom object", async () => {
    const f = fetchMock({ body: { saved: true } });
    globalThis.fetch = f as unknown as typeof fetch;
    await api.putSettings({ foo: 1 });
    const init = f.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ custom: { foo: 1 } });
  });
});
