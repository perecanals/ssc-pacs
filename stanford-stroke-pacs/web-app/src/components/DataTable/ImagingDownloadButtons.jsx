import PropTypes from "prop-types";

const DownloadIcon = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{ display: "inline-block", verticalAlign: "middle" }}
  >
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
);

export default function ImagingDownloadButtons({
  uid,
  busy,
  onDownload,
  className = "link-btn",
}) {
  return (
    <>
      <button
        className={className}
        title="Download DICOM as zip"
        aria-label="Download DICOM as zip"
        disabled={busy}
        onClick={() => onDownload(uid)}
      >
        {busy ? "…" : <DownloadIcon />}
      </button>
      <button
        className={className}
        title="Download as NIfTI (.nii.gz)"
        disabled={busy}
        onClick={() => onDownload(uid, "nifti")}
      >
        NIfTI
      </button>
    </>
  );
}
ImagingDownloadButtons.propTypes = {
  uid: PropTypes.string.isRequired,
  busy: PropTypes.bool,
  onDownload: PropTypes.func.isRequired,
  className: PropTypes.string,
};
