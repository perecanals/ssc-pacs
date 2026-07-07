import { useState, useEffect, useRef } from "react";
import PropTypes from "prop-types";
import { compareSelectValues, normalizeSelectFilterValues } from "../../utils/table";

export default function SelectFilterControl({ options = [], value, onToggle, onClear }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const close = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const selectedValues = normalizeSelectFilterValues(value);
  const uniqueOptions = [...new Set(options.map((option) => String(option).trim()).filter(Boolean))]
    .sort(compareSelectValues);
  const buttonLabel = selectedValues.length === 0
    ? "Any"
    : selectedValues.length === 1
      ? selectedValues[0]
      : `${selectedValues.length} selected`;

  return (
    <div className="dt__select-filter" ref={ref}>
      <button
        type="button"
        className={`dt__select-filter-trigger${selectedValues.length ? " dt__select-filter-trigger--active" : ""}`}
        onClick={() => setOpen((prev) => !prev)}
        title={selectedValues.length ? selectedValues.join(", ") : "Filter by predefined options"}
      >
        <span className="dt__select-filter-label">{buttonLabel}</span>
        <span className="dt__select-filter-caret">{open ? "\u25B2" : "\u25BC"}</span>
      </button>
      {open && (
        <div className="dt__select-filter-menu">
          <div className="dt__select-filter-menu-head">
            <button
              type="button"
              className="dt__select-filter-clear"
              onClick={() => onClear()}
              disabled={selectedValues.length === 0}
            >
              Clear
            </button>
          </div>
          <div className="dt__select-filter-options">
            {uniqueOptions.length === 0 ? (
              <div className="dt__select-filter-empty">No predefined options</div>
            ) : (
              uniqueOptions.map((option) => {
                const checked = selectedValues.includes(option);
                return (
                  <label key={option} className="dt__select-filter-option">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggle(option)}
                      className="dt__select-filter-checkbox"
                    />
                    <span className="dt__select-filter-option-label">{option}</span>
                  </label>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

SelectFilterControl.propTypes = {
  options: PropTypes.arrayOf(PropTypes.string),
  value: PropTypes.oneOfType([PropTypes.string, PropTypes.arrayOf(PropTypes.string)]),
  onToggle: PropTypes.func.isRequired,
  onClear: PropTypes.func.isRequired,
};
