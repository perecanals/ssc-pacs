import { describe, it, expect, vi, beforeEach } from "vitest";
import { apiFetch, apiGet, apiPost, apiDelete } from "../api/client.js";

// Stub window.dispatchEvent and fetch for each test.
beforeEach(() => {
  vi.restoreAllMocks();
});

describe("apiFetch", () => {
  it("prepends the API base and forwards options", async () => {
    const mockResponse = {
      status: 200,
      ok: true,
      json: () => Promise.resolve({ ok: true }),
      headers: new Headers(),
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    await apiFetch("/api/me");

    expect(fetch).toHaveBeenCalledWith(
      "/api/me",
      expect.objectContaining({
        credentials: "same-origin",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
        }),
      }),
    );
  });

  it("dispatches auth:expired on 401", async () => {
    const mockResponse = { status: 401, ok: false };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    await apiFetch("/api/me");

    expect(dispatchSpy).toHaveBeenCalledWith(
      expect.objectContaining({ type: "auth:expired" }),
    );
  });

  it("does not dispatch auth:expired on 200", async () => {
    const mockResponse = { status: 200, ok: true };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    await apiFetch("/api/me");

    expect(dispatchSpy).not.toHaveBeenCalled();
  });
});

describe("apiGet", () => {
  it("returns parsed JSON on success", async () => {
    const data = { username: "test" };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        status: 200,
        ok: true,
        json: () => Promise.resolve(data),
      }),
    );

    const result = await apiGet("/api/me");
    expect(result).toEqual(data);
  });

  it("throws on non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ status: 500, ok: false }),
    );

    await expect(apiGet("/api/fail")).rejects.toThrow("500");
  });
});

describe("apiPost", () => {
  it("sends JSON body with POST method", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ status: 200, ok: true }),
    );

    await apiPost("/api/login", { username: "a", password: "b" });

    expect(fetch).toHaveBeenCalledWith(
      "/api/login",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ username: "a", password: "b" }),
      }),
    );
  });
});

describe("apiDelete", () => {
  it("sends DELETE method", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ status: 204, ok: true }),
    );

    await apiDelete("/api/annotations/1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/annotations/1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
