import { useId, useRef } from "react";
import PropTypes from "prop-types";
import ExistingValues from "./ExistingValues";

function temporalSpec(type) {
  if (type === "date") {
    return {
      type: "date",
      hint: "Format: YYYY-MM-DD (for example, 2026-09-14).",
    };
  }
  if (/^timestamp(?:\(\d+\))? (with|without) time zone$/.test(type)) {
    const utc = type.endsWith("with time zone");
    return {
      type: "datetime-local",
      utc,
      hint: `Format: YYYY-MM-DD HH:mm:ss (for example, 2026-09-14 14:30:00). Optional fractional seconds. ${utc ? "Enter the time in UTC." : "This column stores no timezone."}`,
    };
  }
  return null;
}

// Saved reports may predate the picker and contain a space or an explicit
// UTC offset. Keep the original query value until the user edits it.
function pickerValue(value, spec) {
  const text = String(value ?? "").replace(" ", "T");
  if (spec?.utc && /(?:Z|[+-]\d{2}:\d{2})$/.test(text)) {
    const date = new Date(text);
    if (!Number.isNaN(date.getTime())) {
      const fraction = text.match(/\.(\d+)/)?.[1];
      return date.toISOString().slice(0, 19) + (fraction ? `.${fraction}` : "");
    }
  }
  return text;
}

export default function ConditionValue({
  column,
  operator = "eq",
  dataset = null,
  value,
  onChange,
}) {
  const ref = useRef(null);
  const hintId = useId();
  const spec = temporalSpec(column?.type || "");
  const normalized = pickerValue(value, spec);
  const [whole, fraction = ""] = normalized.split(".");
  const datetime = spec?.type === "datetime-local";
  const combine = (next, digits) => {
    const base = next.split(".")[0];
    if (!base || !digits) return base;
    return `${base.length === 16 ? base + ":00" : base}.${digits}`;
  };
  if (operator === "in")
    return (
      <div className="explorer__condition-value">
        {column?.ref && (
          <ExistingValues
            key={`${column.ref}:in:${dataset || ""}`}
            column={column.ref}
            operator={operator}
            dataset={dataset}
            initiallyOpen
            multiple
            selectedValues={Array.isArray(value) ? value.map(String) : []}
            onChange={onChange}
          />
        )}
        {spec && <small>{spec.hint}</small>}
      </div>
    );
  return (
    <div className="explorer__condition-value">
      <div className="explorer__actions">
        <input
          ref={ref}
          aria-label="Filter value"
          aria-describedby={spec ? hintId : undefined}
          type={spec?.type || "text"}
          step={datetime ? "1" : undefined}
          placeholder={column?.type || "Value"}
          value={datetime ? whole : spec ? normalized : (value ?? "")}
          onChange={(event) =>
            onChange(
              datetime
                ? combine(event.target.value, fraction)
                : event.target.value,
            )
          }
        />
        {datetime && (
          <input
            className="explorer__fraction"
            aria-label="Fractional seconds (optional)"
            title="Optional fractional seconds: up to six digits"
            placeholder="Fraction (optional)"
            inputMode="numeric"
            maxLength={6}
            value={fraction}
            disabled={!whole}
            onChange={(event) => {
              if (/^\d{0,6}$/.test(event.target.value))
                onChange(combine(whole, event.target.value));
            }}
          />
        )}
        {spec && (
          <button
            type="button"
            className="btn-outline"
            aria-label="Open calendar"
            onClick={() => {
              ref.current?.focus();
              try {
                ref.current?.showPicker?.();
              } catch {
                // Browsers without showPicker still support native entry.
              }
            }}
          >
            Calendar
          </button>
        )}
      </div>
      {spec && <small id={hintId}>{spec.hint}</small>}
      {column?.ref && (
        <ExistingValues
          key={`${column.ref}:${operator}:${dataset || ""}`}
          column={column.ref}
          operator={operator}
          initiallyOpen={column.label_datatype === "select"}
          dataset={dataset}
          onChange={onChange}
        />
      )}
    </div>
  );
}

ConditionValue.propTypes = {
  column: PropTypes.object,
  operator: PropTypes.string,
  dataset: PropTypes.string,
  value: PropTypes.oneOfType([
    PropTypes.string,
    PropTypes.number,
    PropTypes.bool,
    PropTypes.array,
  ]),
  onChange: PropTypes.func.isRequired,
};
