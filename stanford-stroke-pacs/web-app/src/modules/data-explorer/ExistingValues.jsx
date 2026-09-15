import { useEffect, useId, useState } from "react";
import PropTypes from "prop-types";
import { apiFetch } from "../../api/client";

export default function ExistingValues({
  column,
  operator,
  onChange,
  initiallyOpen = false,
  dataset = null,
  multiple = false,
  selectedValues = [],
}) {
  const [open, setOpen] = useState(initiallyOpen);
  const [search, setSearch] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [manual, setManual] = useState("");
  const id = useId();
  const toggle = (value) =>
    onChange(
      selectedValues.includes(value)
        ? selectedValues.filter((item) => item !== value)
        : [...selectedValues, value],
    );
  const addManual = () => {
    if (
      manual &&
      !selectedValues.includes(manual) &&
      selectedValues.length < 1000
    )
      onChange([...selectedValues, manual]);
    setManual("");
  };

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ column, operator, search });
        if (dataset) params.set("dataset", dataset);
        const response = await apiFetch(`/api/data-explorer/values?${params}`, {
          signal: controller.signal,
        });
        let data;
        try {
          data = await response.json();
        } catch {
          throw new Error(
            "Existing values are unavailable. Try again or enter a value manually.",
          );
        }
        if (response.status === 404)
          throw new Error(
            "Existing values are unavailable. Try again or enter a value manually.",
          );
        if (!response.ok)
          throw new Error(
            typeof data.detail === "string"
              ? data.detail
              : "Existing values could not be loaded. Enter a value manually.",
          );
        if (!Array.isArray(data.values))
          throw new Error(
            "Existing values could not be loaded. Refresh the page and try again.",
          );
        if (!controller.signal.aborted) setResult(data);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason.message);
      }
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [open, column, operator, search, dataset]);

  return (
    <div className="explorer__existing-values">
      {multiple && (
        <>
          <div
            className="explorer__chosen-values"
            aria-label="Selected filter values"
          >
            {selectedValues.map((value) => (
              <button
                key={value}
                type="button"
                aria-label={`Remove value ${value || "(empty string)"}`}
                onClick={() => toggle(value)}
              >
                {value || "(empty string)"} ×
              </button>
            ))}
          </div>
          <div className="explorer__actions">
            <input
              aria-label="Add filter value"
              placeholder="Add a value manually"
              value={manual}
              onChange={(event) => setManual(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addManual();
                }
              }}
            />
            <button
              type="button"
              disabled={!manual || selectedValues.length >= 1000}
              onClick={addManual}
            >
              Add value
            </button>
          </div>
          <small>
            {selectedValues.length} values selected. Match any selected value
            (IN).
          </small>
        </>
      )}
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          setResult(null);
          setError("");
          setOpen(!open);
        }}
      >
        {open ? "Hide existing values" : "Choose existing value"}
      </button>
      {open && (
        <div id={id} className="explorer__value-picker">
          <input
            type="search"
            aria-label="Search existing values"
            placeholder="Search existing values…"
            maxLength={2000}
            value={search}
            onChange={(event) => {
              setResult(null);
              setError("");
              setSearch(event.target.value);
            }}
          />
          {error ? (
            <p role="alert">{error}</p>
          ) : !result ? (
            <small role="status">Loading existing values…</small>
          ) : result.values.length ? (
            <>
              {multiple ? (
                <div
                  className="explorer__value-options"
                  role="group"
                  aria-label="Existing values"
                >
                  {result.values.map((value) => (
                    <label key={value}>
                      <input
                        type="checkbox"
                        checked={selectedValues.includes(value)}
                        disabled={
                          !selectedValues.includes(value) &&
                          selectedValues.length >= 1000
                        }
                        onChange={() => toggle(value)}
                      />
                      {value === "" ? "(empty string)" : value}
                    </label>
                  ))}
                </div>
              ) : (
                <select
                  aria-label="Existing values"
                  size={Math.min(6, Math.max(2, result.values.length))}
                  value=""
                  onChange={(event) => {
                    onChange(result.values[Number(event.target.value)]);
                    setOpen(false);
                  }}
                >
                  <option value="" disabled hidden>
                    Choose a value
                  </option>
                  {result.values.map((value, index) => (
                    <option key={value} value={String(index)}>
                      {value === "" ? "(empty string)" : value}
                    </option>
                  ))}
                </select>
              )}
              {result.has_more && (
                <small>
                  Showing the first 100 values. Search to narrow the list.
                </small>
              )}
            </>
          ) : (
            <small role="status">
              No matching values. You can enter a value manually.
            </small>
          )}
          <small>
            Values come from this column within{" "}
            {dataset ? `dataset ${dataset}` : "your accessible datasets"}. Other
            conditions do not narrow this list. For NULL, choose “is empty
            (NULL)” as the operator.
          </small>
        </div>
      )}
    </div>
  );
}

ExistingValues.propTypes = {
  column: PropTypes.string.isRequired,
  operator: PropTypes.string.isRequired,
  onChange: PropTypes.func.isRequired,
  initiallyOpen: PropTypes.bool,
  dataset: PropTypes.string,
  multiple: PropTypes.bool,
  selectedValues: PropTypes.arrayOf(PropTypes.string),
};
