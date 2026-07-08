import { useState, useEffect, useRef, useMemo } from "react";
import PropTypes from "prop-types";
import {
  groupByInstrument,
  LEVEL_LABELS,
  LEVEL_ORDER,
} from "../utils/table";
import "./ColumnSelector.css";

export default function ColumnSelector({
  allCols,
  visibleKeys,
  onToggle,
  onSetKeysVisible,
  onEditLabel,
  showStatusColumn = false,
  statusColumnVisible = true,
  onToggleStatusColumn,
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const close = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);

  const builtins = allCols.filter((c) => c.builtin);
  const labels = allCols.filter((c) => !c.builtin);
  const groupByLevel = (cols) =>
    LEVEL_ORDER
      .map((lvl) => ({
        lvl,
        cols: cols.filter((c) => (c.level || "series") === lvl),
      }))
      .filter(({ cols }) => cols.length > 0);

  const builtinGroups = groupByLevel(builtins);
  const instrumentGroups = useMemo(() => groupByInstrument(labels), [labels]);

  const visibleSet = useMemo(() => new Set(visibleKeys), [visibleKeys]);

  return (
    <div className="col-selector" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="pill-btn col-selector__trigger"
      >
        Displayed Columns &#9662;
      </button>
      {open && (
        <div className="col-selector__dropdown">
          <div className="col-selector__section-title">
            Data columns
          </div>
          {builtinGroups.map(({ lvl, cols }, idx) => (
            <div key={`builtin-${lvl}`}>
              {idx > 0 && <div className="col-selector__divider" />}
              <div className="col-selector__subgroup-title">
                {LEVEL_LABELS[lvl] || lvl} columns
              </div>
              {cols.map((c) => (
                <label key={c.key} className="col-selector__item">
                  <input
                    type="checkbox"
                    checked={visibleSet.has(c.key)}
                    onChange={() => onToggle(c.key)}
                  />
                  {c.label}
                </label>
              ))}
              {showStatusColumn && lvl === "patient" && (
                <label className="col-selector__item">
                  <input
                    type="checkbox"
                    checked={statusColumnVisible}
                    onChange={onToggleStatusColumn}
                  />
                  Status
                </label>
              )}
            </div>
          ))}
          <div className="col-selector__divider" />
          <div className="col-selector__section-title">
            Annotation labels
          </div>
          {instrumentGroups.length === 0 && (
            <div className="col-selector__empty">No labels defined yet.</div>
          )}
          {instrumentGroups.map(({ key, name, items: cols }) => {
            const keys = cols.map((c) => c.key);
            const visibleCount = keys.filter((k) => visibleSet.has(k)).length;
            const allVisible = visibleCount === keys.length;
            return (
              <div key={`instr-${key}`}>
                <div className="col-selector__instrument-header">
                  <span className="col-selector__instrument-name">
                    {name}{" "}
                    <span className="col-selector__instrument-count">
                      ({cols.length})
                    </span>
                  </span>
                  <button
                    type="button"
                    onClick={() => onSetKeysVisible(keys, !allVisible)}
                    className="col-selector__bulk-btn"
                  >
                    {allVisible ? "Hide all" : "Show all"}
                  </button>
                </div>
                {cols.map((c) => (
                  <div key={c.key} className="col-selector__item-row">
                    <label
                      title={c.description || ""}
                      className="col-selector__item col-selector__item--flex"
                    >
                      <input
                        type="checkbox"
                        checked={visibleSet.has(c.key)}
                        onChange={() => onToggle(c.key)}
                      />
                      <span className="col-selector__label-text">
                        {c.label}{" "}
                        <span className="col-selector__datatype">
                          ({c.datatype} · {c.level})
                        </span>
                      </span>
                    </label>
                    {onEditLabel && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onEditLabel(c.labelDef);
                        }}
                        title="Edit label"
                        className="col-selector__edit-btn"
                      >
                        {"✎"}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

ColumnSelector.propTypes = {
  allCols: PropTypes.array.isRequired,
  visibleKeys: PropTypes.arrayOf(PropTypes.string).isRequired,
  onToggle: PropTypes.func.isRequired,
  onSetKeysVisible: PropTypes.func.isRequired,
  onEditLabel: PropTypes.func,
  showStatusColumn: PropTypes.bool,
  statusColumnVisible: PropTypes.bool,
  onToggleStatusColumn: PropTypes.func,
};
