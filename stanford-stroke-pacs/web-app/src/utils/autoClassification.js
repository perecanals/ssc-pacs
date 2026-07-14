// Provenance for the machine-derived "Auto ..." columns.

// These anchors are fixed offsets, not recorded times (see _ANCHOR_PRECEDENCE in
// image_ingestion_protocols/series_classification.py), so any timepoint derived
// from one is an estimate.
const ESTIMATED_ANCHORS = new Set([
  "receiving_arrival_time",
  "time_recognized",
]);

export function isEstimatedAnchor(source) {
  return ESTIMATED_ANCHORS.has(source);
}

// Multi-line hover text for an Auto cell; undefined for any other column.
export function buildAutoTooltip(sourceKey, row) {
  if (!row) return undefined;

  if (sourceKey === "series_type") {
    // A NULL series_type is a decision, not a failure: the classifier excluded
    // the series (bone, topogram, RAPID output, ...) and series_type_rule says
    // which exclusion fired. That is most of the corpus, so surface the reason
    // rather than leaving an empty cell.
    if (!row.series_type) {
      return row.series_type_rule
        ? `Excluded by the classifier\nrule: ${row.series_type_rule}`
        : undefined;
    }
    const parts = ["Machine-derived, not a human label"];
    if (row.series_label) parts.push(`label: ${row.series_label}`);
    if (row.series_type_rank != null) {
      parts.push(
        row.series_type_rank === 1
          ? `rank 1 — the ${row.series_type} to use for this patient`
          : `rank ${row.series_type_rank} of this patient's ${row.series_type} series`,
      );
    }
    if (row.series_type_rule) parts.push(`rule: ${row.series_type_rule}`);
    if (row.series_type_version)
      parts.push(`version: ${row.series_type_version}`);
    return parts.join("\n");
  }

  if (sourceKey === "timepoint") {
    if (!row.timepoint) return undefined;
    const parts = ["Machine-derived, not a human label"];
    const anchor = row.timepoint_anchor_source;
    if (anchor) {
      parts.push(
        isEstimatedAnchor(anchor)
          ? `anchor: ${anchor} — ESTIMATED (offset, not a recorded puncture time)`
          : `anchor: ${anchor}`,
      );
    }
    if (row.hours_to_event != null) {
      const h = Math.round(row.hours_to_event * 10) / 10;
      parts.push(`${h >= 0 ? "+" : ""}${h} h from anchor`);
    }
    if (row.timepoint_version) parts.push(`version: ${row.timepoint_version}`);
    return parts.join("\n");
  }

  return undefined;
}
