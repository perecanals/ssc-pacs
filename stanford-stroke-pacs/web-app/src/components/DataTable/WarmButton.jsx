import PropTypes from "prop-types";

// Decompress / readiness control shown on patient and study rows in
// cold_path_cache mode. Doubles as both the trigger and the persistent
// readiness badge: a single `status` (study) or `summary` (patient aggregate)
// drives label, color, and whether a click queues a warm.

// Collapse either a single study status or a patient aggregate into one
// display state. An un-polled patient (summary undefined) reads as cold so the
// row still offers a "Decompress" affordance. "queued" means the user asked to
// warm but no worker has started yet (bridges the warm_executor queue gap).
function deriveState(status, summary) {
  if (summary) {
    const { total = 0, hot = 0, warming = 0, queued = 0, error = 0 } = summary;
    if (total === 0) return "cold";
    if (warming > 0) return "warming";
    if (queued > 0) return "queued";
    if (error > 0) return "error";
    if (hot >= total) return "hot";
    return "cold";
  }
  return status || "cold";
}

export default function WarmButton({ status, summary, onWarm, baseClass }) {
  const state = deriveState(status, summary);
  const label = {
    cold: "Decompress",
    queued: "Queued…",
    warming: "Warming…",
    hot: "Ready",
    error: "Retry",
  }[state];
  const title = {
    cold: "Decompress imaging now so the viewer opens instantly later",
    queued: "Queued for decompression…",
    warming: "Decompressing imaging…",
    hot: "Imaging is decompressed and ready",
    error: "Decompression failed — click to retry",
  }[state];
  const actionable = state === "cold" || state === "error";
  return (
    <button
      type="button"
      onClick={actionable ? onWarm : undefined}
      disabled={!actionable}
      title={title}
      className={`${baseClass} dt__warm-btn dt__warm-btn--${state}`}
    >
      {label}
    </button>
  );
}

WarmButton.propTypes = {
  status: PropTypes.oneOf(["cold", "queued", "warming", "hot", "error"]),
  summary: PropTypes.shape({
    total: PropTypes.number,
    cold: PropTypes.number,
    warming: PropTypes.number,
    hot: PropTypes.number,
    error: PropTypes.number,
    queued: PropTypes.number,
  }),
  onWarm: PropTypes.func.isRequired,
  baseClass: PropTypes.string,
};

WarmButton.defaultProps = {
  baseClass: "link-btn",
};
