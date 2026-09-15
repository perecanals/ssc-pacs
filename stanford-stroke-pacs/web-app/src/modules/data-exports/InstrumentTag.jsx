import PropTypes from "prop-types";

// Saturated text colors on the shared light surface. A name's color stays stable
// when tables, search terms or the selected instrument change.
const COLORS = [
  "#1d4ed8",
  "#047857",
  "#a21caf",
  "#b45309",
  "#0e7490",
  "#be123c",
  "#6d28d9",
  "#c2410c",
];
export default function InstrumentTag({ name }) {
  let hash = 0;
  for (const char of name || "") hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return (
    <span
      className="data-exports__column-instrument"
      style={{
        color: name ? COLORS[hash % COLORS.length] : "#6d28d9",
      }}
      title={`Instrument: ${name || "Unassigned"}`}
    >
      [{name || "Unassigned"}]
    </span>
  );
}
InstrumentTag.propTypes = { name: PropTypes.string };
