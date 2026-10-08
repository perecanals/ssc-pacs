import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// resolveOhifViewerUrl must queue a background warm of the rest of the study
// on a *series* preview before handing back the iframe URL: OHIF requests
// metadata for every series in the study, and cold siblings must be at least
// 'queued' by then so the DICOMweb proxy holds their metadata requests
// instead of letting Orthanc 500 (the "Something went wrong" toasts).
//
// warmOhif caches the storage mode in module scope, so each test imports a
// fresh module instance.

function jsonResponse(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => data,
    text: async () => JSON.stringify(data),
  };
}

const STUDY = "9.9.9";
const SERIES = "1.2.3";

function stubFetch({ studyStatus }) {
  const calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, options = {}) => {
      calls.push({ url, method: options.method || "GET" });
      if (url.startsWith(`/api/ohif-link/${STUDY}`)) {
        return jsonResponse({ url: "/ohif/viewer?x=1" });
      }
      if (url === "/api/storage-mode") {
        return jsonResponse({ storage_mode: "cold_path_cache" });
      }
      if (url === "/api/cache-status/batch") {
        return jsonResponse({
          studies: { [STUDY]: studyStatus },
          patients: {},
          series: {},
        });
      }
      if (url === `/api/studies/${STUDY}/warm`) {
        return jsonResponse({ ok: true, queued: true }, 202);
      }
      throw new Error(`unexpected fetch: ${url}`);
    }),
  );
  return calls;
}

async function freshModule() {
  vi.resetModules();
  return import("../api/warmOhif");
}

describe("resolveOhifViewerUrl sibling warm queueing", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("queues a study warm before returning a series-preview URL when the study is cold", async () => {
    const calls = stubFetch({ studyStatus: "cold" });
    const { resolveOhifViewerUrl } = await freshModule();
    const url = await resolveOhifViewerUrl(STUDY, SERIES);
    expect(url).toBe("/ohif/viewer?x=1");
    const warmCall = calls.find((c) => c.url === `/api/studies/${STUDY}/warm`);
    expect(warmCall).toBeTruthy();
    expect(warmCall.method).toBe("POST");
  });

  it("does not re-queue when the study is already hot", async () => {
    const calls = stubFetch({ studyStatus: "hot" });
    const { resolveOhifViewerUrl } = await freshModule();
    await resolveOhifViewerUrl(STUDY, SERIES);
    expect(calls.some((c) => c.url === `/api/studies/${STUDY}/warm`)).toBe(
      false,
    );
  });

  it("does not re-queue when a warm is already in flight", async () => {
    const calls = stubFetch({ studyStatus: "warming" });
    const { resolveOhifViewerUrl } = await freshModule();
    await resolveOhifViewerUrl(STUDY, SERIES);
    expect(calls.some((c) => c.url === `/api/studies/${STUDY}/warm`)).toBe(
      false,
    );
  });

  it("leaves study opens untouched (no batch status, no sibling warm)", async () => {
    const calls = stubFetch({ studyStatus: "cold" });
    const { resolveOhifViewerUrl } = await freshModule();
    const url = await resolveOhifViewerUrl(STUDY);
    expect(url).toBe("/ohif/viewer?x=1");
    expect(calls.map((c) => c.url)).toEqual([`/api/ohif-link/${STUDY}`]);
  });

  it("still returns the URL when the sibling warm queueing fails", async () => {
    const calls = stubFetch({ studyStatus: "cold" });
    const { resolveOhifViewerUrl } = await freshModule();
    // Make the warm POST blow up; the preview must not care.
    const inner = global.fetch;
    vi.stubGlobal("fetch", async (url, options) => {
      if (url === `/api/studies/${STUDY}/warm`) throw new Error("boom");
      return inner(url, options);
    });
    const url = await resolveOhifViewerUrl(STUDY, SERIES);
    expect(url).toBe("/ohif/viewer?x=1");
    expect(calls.some((c) => c.url === "/api/cache-status/batch")).toBe(true);
  });
});

describe("getBatchCacheStatus chunking", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  function stubBatch() {
    const bodies = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url, options = {}) => {
        if (url !== "/api/cache-status/batch") {
          throw new Error(`unexpected fetch: ${url}`);
        }
        const body = JSON.parse(options.body);
        bodies.push(body);
        return jsonResponse({
          studies: Object.fromEntries(body.uids.map((u) => [u, "hot"])),
          patients: Object.fromEntries(
            body.patient_keys.map((p) => [p, { total: 1, hot: 1 }]),
          ),
          series: Object.fromEntries(body.series_uids.map((s) => [s, "cold"])),
        });
      }),
    );
    return bodies;
  }

  it("sends one request when every list fits the server cap", async () => {
    const bodies = stubBatch();
    const { getBatchCacheStatus, BATCH_CACHE_STATUS_MAX_IDS } =
      await freshModule();
    const uids = Array.from(
      { length: BATCH_CACHE_STATUS_MAX_IDS },
      (_, i) => `s${i}`,
    );
    const data = await getBatchCacheStatus(uids, ["p1"], []);
    expect(bodies).toHaveLength(1);
    expect(Object.keys(data.studies)).toHaveLength(BATCH_CACHE_STATUS_MAX_IDS);
    expect(data.patients).toEqual({ p1: { total: 1, hot: 1 } });
  });

  it("splits lists above the cap into several requests and merges the maps", async () => {
    const bodies = stubBatch();
    const { getBatchCacheStatus, BATCH_CACHE_STATUS_MAX_IDS } =
      await freshModule();
    const n = BATCH_CACHE_STATUS_MAX_IDS;
    const uids = Array.from({ length: 2 * n + 1 }, (_, i) => `s${i}`);
    const series = Array.from({ length: n + 1 }, (_, i) => `se${i}`);
    const data = await getBatchCacheStatus(uids, ["p1"], series);
    expect(bodies).toHaveLength(3);
    for (const b of bodies) {
      expect(b.uids.length).toBeLessThanOrEqual(n);
      expect(b.series_uids.length).toBeLessThanOrEqual(n);
    }
    expect(Object.keys(data.studies)).toHaveLength(2 * n + 1);
    expect(Object.keys(data.series)).toHaveLength(n + 1);
    expect(data.patients).toEqual({ p1: { total: 1, hot: 1 } });
  });

  it("makes no request when all lists are empty", async () => {
    const bodies = stubBatch();
    const { getBatchCacheStatus } = await freshModule();
    const data = await getBatchCacheStatus([], [], []);
    expect(bodies).toHaveLength(0);
    expect(data).toEqual({ studies: {}, patients: {}, series: {} });
  });
});
