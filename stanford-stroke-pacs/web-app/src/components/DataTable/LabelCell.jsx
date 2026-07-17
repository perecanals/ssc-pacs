import PropTypes from "prop-types";
import InlineEdit from "../InlineEdit";

// One annotation-label cell. The counterpart to BuiltinCell: builtin columns
// render a machine/upstream value, label columns render an editor.
//
// Shared by all three tables (main rows, child rows, grandchild rows) so the
// column -> InlineEdit wiring — including `labelDef`, which decides whether the
// cell is editable at all — exists once rather than three times.
export default function LabelCell({
  col,
  entity,
  annotations,
  onMutated,
  levelFallback,
}) {
  return (
    <InlineEdit
      level={col.level || levelFallback}
      entity={entity}
      labelName={col.key.replace("label:", "")}
      datatype={col.datatype}
      defOptions={col.options || []}
      annotations={annotations}
      onMutated={onMutated}
      labelDef={col.labelDef}
    />
  );
}

LabelCell.propTypes = {
  col: PropTypes.object.isRequired,
  entity: PropTypes.object.isRequired,
  annotations: PropTypes.array.isRequired,
  onMutated: PropTypes.func.isRequired,
  // Used only when the column carries no level of its own.
  levelFallback: PropTypes.oneOf(["patient", "study", "series"]),
};
