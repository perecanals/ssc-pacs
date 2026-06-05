import { apiGet, apiFetch } from "./client";

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
 * Poll cache-status until the study is hot. Returns true once hot, false on
 * timeout. Throws if the backend reports an error status.
 */
async function pollCacheUntilHot(studyinstanceuid, maxMs = 600_000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    const st = await apiGet(`/api/studies/${encodeURIComponent(studyinstanceuid)}/cache-status`);
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
 * Returns OHIF viewer URL. If the study is cold, warms it first (transparently).
 * If the study is already warming, polls until hot then fetches the URL.
 */
export async function resolveOhifViewerUrl(studyinstanceuid, seriesinstanceuid = null) {
  const path = buildOhifLinkPath(studyinstanceuid, seriesinstanceuid);

  const res = await apiFetch(path);
  if (!res.ok) throw new Error(`OHIF link failed: ${res.status}`);
  const data = await res.json();

  // Study is ready — return URL directly.
  if (data.url) return data.url;

  // Study needs warming — trigger it (or join an in-progress warm) then retry.
  if (data.status === "cold" || data.status === "warming") {
    if (data.status === "cold") {
      await warmStudy(studyinstanceuid);
    } else {
      // Already warming — just poll until hot (retry below settles a timeout).
      await pollCacheUntilHot(studyinstanceuid);
    }

    const retryRes = await apiFetch(path);
    if (!retryRes.ok) throw new Error(`OHIF link failed: ${retryRes.status}`);
    const retryData = await retryRes.json();
    if (!retryData.url) throw new Error(retryData.detail || "Study still not ready after warming");
    return retryData.url;
  }

  throw new Error(data.detail || data.status || "No OHIF URL returned");
}
