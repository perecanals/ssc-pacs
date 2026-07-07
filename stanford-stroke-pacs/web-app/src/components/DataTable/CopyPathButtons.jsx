import { useEffect, useRef, useState } from "react";
import PropTypes from "prop-types";
import { apiGet } from "../../api/client";

// Admin-only quick actions rendered next to the DICOM download button: copy a
// series' filesystem paths (loose DICOM directory / compressed archive) to
// the clipboard. Paths are fetched at click time from the admin-gated
// /api/series/{uid}/paths endpoint, so row payloads never carry server paths.

const FolderIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ display: "inline-block", verticalAlign: "middle" }}>
    <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
  </svg>
);

const ArchiveIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ display: "inline-block", verticalAlign: "middle" }}>
    <rect width="20" height="5" x="2" y="3" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" />
  </svg>
);

async function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  // Fallback for non-secure contexts, where the async clipboard API is absent.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
}

const KINDS = [
  {
    kind: "dicom",
    field: "dicom_dir_path",
    title: "Copy DICOM directory path",
    missing: "No DICOM directory path recorded for this series",
    Icon: FolderIcon,
  },
  {
    kind: "archive",
    field: "dicom_archive_path",
    title: "Copy compressed archive path",
    missing: "No archive path recorded for this series",
    Icon: ArchiveIcon,
  },
];

export default function CopyPathButtons({ seriesUid, baseClass = "link-btn" }) {
  // Which button is flashing its "copied" checkmark, if any.
  const [copied, setCopied] = useState(null);
  const flashTimer = useRef(null);
  useEffect(() => () => clearTimeout(flashTimer.current), []);

  const handleCopy = async ({ kind, field, missing }) => {
    try {
      const paths = await apiGet(`/api/series/${encodeURIComponent(seriesUid)}/paths`);
      const path = paths?.[field];
      if (!path) {
        alert(missing);
        return;
      }
      await copyToClipboard(path);
      setCopied(kind);
      clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setCopied(null), 1200);
    } catch {
      alert("Could not copy path");
    }
  };

  return (
    <>
      {KINDS.map(({ Icon, ...k }) => (
        <button key={k.kind} onClick={() => handleCopy(k)} className={baseClass} title={k.title}>
          {copied === k.kind ? "✓" : <Icon />}
        </button>
      ))}
    </>
  );
}

CopyPathButtons.propTypes = {
  seriesUid: PropTypes.string.isRequired,
  baseClass: PropTypes.string,
};
