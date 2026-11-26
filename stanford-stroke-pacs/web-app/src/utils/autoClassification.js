// Provenance for the machine-derived "Auto ..." columns.

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
    if (row.timepoint_details) {
      const parts = [row.timepoint || "Undetermined"];
      if (row.general_timepoint && row.general_timepoint !== row.timepoint)
        parts.push(`General timepoint: ${row.general_timepoint}`);
      return [...parts, ...row.timepoint_details].join("\n");
    }
    if (!row.timepoint) return undefined;
    const parts = ["Machine-derived, not a human label"];
    if (row.general_timepoint && row.general_timepoint !== row.timepoint)
      parts.push(`general timepoint: ${row.general_timepoint}`);
    const anchor = row.timepoint_anchor_source;
    if (anchor) {
      parts.push(
        row.timepoint_estimated
          ? `anchor: ${anchor} — ESTIMATED (offset, not a recorded puncture time)`
          : `anchor: ${anchor}`,
      );
    }
    if (row.hours_to_event != null) {
      const h = Math.round(row.hours_to_event * 10) / 10;
      parts.push(`${h >= 0 ? "+" : ""}${h} h from anchor`);
    }
    if (row.timepoint_version)
      parts.push(`general timepoint version: ${row.timepoint_version}`);
    return parts.join("\n");
  }

  return undefined;
}
