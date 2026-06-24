import { apiGet, apiFetch, apiPost } from "./client";

function buildOhifLinkPath(studyinstanceuid, seriesinstanceuid) {
  const params = new URLSearchParams();
  if (seriesinstanceuid) params.set("seriesinstanceuid", seriesinstanceuid);
  const q = params.toString();
  return `/api/ohif-link/${encodeURIComponent(studyinstanceuid)}${q ? `?${q}` : ""}`;
}

export async function getStorageMode() {
  try {
    const res = await apiFetch("/api/storage-mode");
    if (!res.ok) return "legacy";
    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("application/json")) return "legacy";
    const data = await res.json();
    return data.storage_mode || "legacy";
  } catch {
    return "legacy";
  }
}

/**
 * Poll cache-status until hot. Returns true once hot, false on timeout. Throws
 * if the backend reports an error status. `kind` selects the study or series
 * cache-status endpoint so a series preview waits only on its own series.
 */
async function pollCacheUntilHot(uid, maxMs = 600_000, kind = "studies") {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    const st = await apiGet(`/api/${kind}/${encodeURIComponent(uid)}/cache-status`);
    if (st.status === "hot") return true;
    if (st.status === "error") throw new Error(st.error_message || "Cache warming failed");
    await new Promise((r) => setTimeout(r, 2000));
  }
  return false;
}

/**
 * POST warm and poll cache-status until hot. Throws on auth failure, backend
 * error, or timeout. Called automatically by resolveOhifViewerUrl when the
 * study is cold; can also be called explicitly.
 */
export async function warmStudy(studyinstanceuid) {
  const warmRes = await apiFetch(
    `/api/studies/${encodeURIComponent(studyinstanceuid)}/warm`,
    { method: "POST" },
  );
  if (!warmRes.ok) {
    if (warmRes.status === 401) throw new Error("Log in to warm imaging cache.");
    const t = await warmRes.text();
    throw new Error(t || "Warm request failed");
  }

  if (!(await pollCacheUntilHot(studyinstanceuid))) throw new Error("Warming timed out");
}

/**
 * POST warm and poll until hot for a single series (the per-series analogue of
 * warmStudy). Used by the series preview so it never waits on the whole study.
 */
export async function warmSeriesSync(seriesinstanceuid) {
  const warmRes = await apiFetch(
    `/api/series/${encodeURIComponent(seriesinstanceuid)}/warm`,
    { method: "POST" },
  );
  if (!warmRes.ok) {
    if (warmRes.status === 401) throw new Error("Log in to warm imaging cache.");
    const t = await warmRes.text();
    throw new Error(t || "Warm request failed");
  }
  if (!(await pollCacheUntilHot(seriesinstanceuid, 600_000, "series"))) {
    throw new Error("Warming timed out");
  }
}

/**
 * Fire-and-forget warm of a single study. Returns once the 202 is accepted;
 * progress is observed separately via batch cache-status polling. Throws on
 * auth failure or insufficient disk space so the caller can surface it.
 */
export async function queueWarmStudy(studyinstanceuid) {
  const res = await apiFetch(
    `/api/studies/${encodeURIComponent(studyinstanceuid)}/warm`,
    { method: "POST" },
  );
  if (!res.ok) {
    if (res.status === 401) throw new Error("Log in to decompress studies.");
    if (res.status === 507) throw new Error("Not enough disk space to decompress this study.");
    throw new Error((await res.text()) || "Decompress request failed");
  }
}

/**
 * Fire-and-forget warm of a single series. Returns once the 202 is accepted;
 * progress is observed via batch cache-status polling (series map). Throws on
 * auth failure or insufficient disk space so the caller can surface it.
 */
export async function queueWarmSeries(seriesinstanceuid) {
  const res = await apiFetch(
    `/api/series/${encodeURIComponent(seriesinstanceuid)}/warm`,
    { method: "POST" },
  );
  if (!res.ok) {
    if (res.status === 401) throw new Error("Log in to decompress series.");
    if (res.status === 507) throw new Error("Not enough disk space to decompress this series.");
    throw new Error((await res.text()) || "Decompress request failed");
  }
}

/**
 * Fire-and-forget warm of every study under a patient. Returns the queued
 * count from the 202 response.
 */
export async function queueWarmPatient(patientId) {
  const res = await apiPost(`/api/patients/${encodeURIComponent(patientId)}/warm`);
  if (!res.ok) {
    if (res.status === 401) throw new Error("Log in to decompress studies.");
    throw new Error((await res.text()) || "Decompress request failed");
  }
  return res.json();
}

/** Aggregate cache status for a patient: {total, cold, warming, hot, error}. */
export async function getPatientCacheStatus(patientId) {
  return apiGet(`/api/patients/${encodeURIComponent(patientId)}/cache-status`);
}

/**
 * Cache status for many studies, patients, and/or series in one request.
 * Returns {studies: {uid: status}, patients: {id: {total, cold, warming, hot,
 * error}}, series: {uid: status}}.
 */
export async function getBatchCacheStatus(uids = [], patientIds = [], seriesUids = []) {
  if (uids.length === 0 && patientIds.length === 0 && seriesUids.length === 0) {
    return { studies: {}, patients: {}, series: {} };
  }
  const res = await apiPost("/api/cache-status/batch", {
    uids,
    patient_ids: patientIds,
    series_uids: seriesUids,
  });
  if (!res.ok) throw new Error(`Batch cache-status failed: ${res.status}`);
  return res.json();
}

/**
 * Returns OHIF viewer URL. If cold, warms first (transparently); if already
 * warming, polls until hot then fetches the URL.
 *
 * Granularity follows the request: a *series* preview (seriesinstanceuid given)
 * warms and waits on just that series, so sifting through individual series is
 * fast. A study open (no series UID) warms the whole study — unchanged.
 */
export async function resolveOhifViewerUrl(studyinstanceuid, seriesinstanceuid = null) {
  const path = buildOhifLinkPath(studyinstanceuid, seriesinstanceuid);

  const res = await apiFetch(path);
  if (!res.ok) throw new Error(`OHIF link failed: ${res.status}`);
  const data = await res.json();

  // Ready — return URL directly.
  if (data.url) return data.url;

  // Needs warming — trigger it (or join an in-progress warm) then retry. The
  // ohif-link status reflects the series when a series UID was passed, so we warm
  // at that same granularity.
  if (data.status === "cold" || data.status === "warming" || data.status === "queued") {
    if (data.status === "cold") {
      if (seriesinstanceuid) await warmSeriesSync(seriesinstanceuid);
      else await warmStudy(studyinstanceuid);
    } else if (seriesinstanceuid) {
      await pollCacheUntilHot(seriesinstanceuid, 600_000, "series");
    } else {
      await pollCacheUntilHot(studyinstanceuid);
    }

    const retryRes = await apiFetch(path);
    if (!retryRes.ok) throw new Error(`OHIF link failed: ${retryRes.status}`);
    const retryData = await retryRes.json();
    if (!retryData.url) throw new Error(retryData.detail || "Imaging still not ready after warming");
    return retryData.url;
  }

  throw new Error(data.detail || data.status || "No OHIF URL returned");
}
