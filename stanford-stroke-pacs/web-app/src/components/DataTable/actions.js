import { apiFetch } from "../../api/client";
import { resolveOhifViewerUrl } from "../../api/warmOhif";

export async function downloadDicomZip(seriesinstanceuid) {
  const res = await apiFetch(`/api/series/${encodeURIComponent(seriesinstanceuid)}/dicom-zip`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  const cd = res.headers.get("Content-Disposition") || "";
  const match = cd.match(/filename="?([^"]+)"?/);
  const fname = match ? match[1] : `${seriesinstanceuid}.zip`;
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

export async function resolveOhifLink(studyinstanceuid, seriesinstanceuid = null) {
  const url = await resolveOhifViewerUrl(studyinstanceuid, seriesinstanceuid);
  if (url) window.open(url, "_blank");
}

export async function refreshLabelledTables() {
  const res = await apiFetch("/api/labelled-tables/refresh", { method: "POST" });
  if (!res.ok) throw new Error("Failed to refresh labelled tables");
  return res.json();
}
