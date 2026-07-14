import PropTypes from "prop-types";
import { formatBuiltinValue } from "../../utils/table";
import { valueColor } from "../../utils/colors";
import {
  buildAutoTooltip,
  isEstimatedAnchor,
} from "../../utils/autoClassification";
import "./BuiltinCell.css";

// Machine-derived value. Same hash colour as the editable SelectPill so equal
// values group by eye, but outlined and muted so it never reads as editable.
//
// `rank` is the per-patient preference rank (rank 1 = the series of that type to
// open). It rides as a badge rather than being folded into the value, so the
// colour stays keyed on the type: every NCCT looks like an NCCT.
export function AutoPill({ value, title, rank = null, estimated = false }) {
  if (!value) return null;
  const c = valueColor(value);
  return (
    <span
      className={`auto-pill${estimated ? " auto-pill--estimated" : ""}`}
      style={{ color: c.text, borderColor: c.text }}
      title={title}
    >
      {value}
      {rank != null && (
        <span
          className={`auto-pill__rank${rank === 1 ? " auto-pill__rank--primary" : ""}`}
        >
          {rank}
        </span>
      )}
      {estimated && (
        <span className="auto-pill__est" aria-hidden="true">
          ~
        </span>
      )}
    </span>
  );
}

AutoPill.propTypes = {
  value: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
  title: PropTypes.string,
  rank: PropTypes.number,
  estimated: PropTypes.bool,
};

// The single place a builtin column's cell contents are produced (main table,
// child table, grandchild table).
export default function BuiltinCell({ col, row }) {
  const raw = row[col.sourceKey] ?? "";
  if (col.readOnlyAuto) {
    const isSeriesType = col.sourceKey === "series_type";
    const tooltip = buildAutoTooltip(col.sourceKey, row);

    // Most series are deliberately excluded rather than unclassified, and the
    // rule says why. Mark them, faintly, so the reason is one hover away.
    if (!raw && isSeriesType && row.series_type_rule) {
      return (
        <span className="auto-pill__excluded" title={tooltip}>
          {"—"}
        </span>
      );
    }

    return (
      <AutoPill
        value={raw}
        title={tooltip}
        rank={isSeriesType ? (row.series_type_rank ?? null) : null}
        estimated={
          !isSeriesType && isEstimatedAnchor(row.timepoint_anchor_source)
        }
      />
    );
  }
  return formatBuiltinValue(col.sourceKey, raw);
}

BuiltinCell.propTypes = {
  col: PropTypes.object.isRequired,
  row: PropTypes.object.isRequired,
};
