import { apiFetch } from "../../api/client";
import { resolveOhifViewerUrl } from "../../api/warmOhif";

export function downloadFilename(disposition, fallback) {
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim());
    } catch {
      /* Use ASCII fallback. */
    }
  }
  const plain = disposition.match(/filename=(?:"([^"\r\n]*)"|([^;\r\n]*))/i);
  return (plain?.[1] ?? plain?.[2])?.trim() || fallback;
}

export const downloadDicomZip = (uid) => downloadSeries(uid, "dicom-zip");
export const downloadNifti = (uid) => downloadSeries(uid, "nifti");

async function downloadSeries(seriesinstanceuid, format) {
  const res = await apiFetch(
    `/api/series/${encodeURIComponent(seriesinstanceuid)}/${format}`,
  );
  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    throw new Error(
      typeof error.detail === "string"
        ? error.detail
        : res.statusText || "Download failed",
    );
  }
  const cd = res.headers.get("Content-Disposition") || "";
  const fname = downloadFilename(
    cd,
    `${seriesinstanceuid}.${format === "nifti" ? "nii.gz" : "zip"}`,
  );
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fname;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export async function resolveOhifLink(
  studyinstanceuid,
  seriesinstanceuid = null,
) {
  const url = await resolveOhifViewerUrl(studyinstanceuid, seriesinstanceuid);
  if (url) window.open(url, "_blank");
}

export async function refreshLabelledTables() {
  const res = await apiFetch("/api/labelled-tables/refresh", {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to refresh labelled tables");
  return res.json();
}

// --- admin: destructive study/series deletion (Orthanc + DB + files) ---

const DELETE_BASE = { study: "studies", series: "series" };

export async function fetchDeletionPlan(level, uid) {
  const res = await apiFetch(
    `/api/admin/${DELETE_BASE[level]}/${encodeURIComponent(uid)}/deletion-plan`,
  );
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

export async function deleteEntity(level, uid) {
  const res = await apiFetch(
    `/api/admin/${DELETE_BASE[level]}/${encodeURIComponent(uid)}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}
