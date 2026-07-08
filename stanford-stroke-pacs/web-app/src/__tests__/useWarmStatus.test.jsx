import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const getBatchCacheStatus = vi.fn();
vi.mock("../api/warmOhif", () => ({
  getBatchCacheStatus: (...a) => getBatchCacheStatus(...a),
  queueWarmStudy: vi.fn().mockResolvedValue(),
  queueWarmSeries: vi.fn().mockResolvedValue(),
  queueWarmPatient: vi.fn().mockResolvedValue(),
}));

import useWarmStatus from "../components/DataTable/useWarmStatus";

// Fresh objects every call, like a real JSON response.
function payload(overrides = {}) {
  return {
    studies: { s1: "cold", s2: "hot" },
    series: { se1: "hot" },
    patients: { p1: { total: 2, hot: 1, warming: 0, queued: 0, error: 0, cold: 1 } },
    ...overrides,
  };
}

// Regression: each 4 s poll used to rebuild all three state objects even when
// nothing changed, re-rendering the whole table while idle. State identity
// must be stable across a no-change poll.
describe("useWarmStatus no-change poll bail-out", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getBatchCacheStatus.mockReset();
    getBatchCacheStatus.mockImplementation(async () => payload());
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function renderWarmStatus() {
    return renderHook(() =>
      useWarmStatus({
        enabled: true,
        studyUids: ["s1", "s2"],
        patientIds: ["p1"],
        seriesUids: ["se1"],
      }),
    );
  }

  it("keeps state identity stable across a no-change poll", async () => {
    const { result } = renderWarmStatus();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0); // flush the mount poll
    });
    const study = result.current.studyStatus;
    const patient = result.current.patientStatus;
    const series = result.current.seriesStatus;
    expect(study.s1).toBe("cold");
    expect(patient.p1.total).toBe(2);
    expect(series.se1).toBe("hot");

    const callsBefore = getBatchCacheStatus.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000); // one interval tick, same data
    });
    expect(getBatchCacheStatus.mock.calls.length).toBeGreaterThan(callsBefore);
    expect(result.current.studyStatus).toBe(study);
    expect(result.current.patientStatus).toBe(patient);
    expect(result.current.seriesStatus).toBe(series);
  });

  it("still updates state when a poll reports a change", async () => {
    const { result } = renderWarmStatus();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const study = result.current.studyStatus;

    getBatchCacheStatus.mockImplementation(async () =>
      payload({ studies: { s1: "hot", s2: "hot" } }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(result.current.studyStatus).not.toBe(study);
    expect(result.current.studyStatus.s1).toBe("hot");
  });
});
