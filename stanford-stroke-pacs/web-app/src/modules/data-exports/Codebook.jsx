import { useEffect, useId, useRef, useState } from "react";
import PropTypes from "prop-types";
import InstrumentTag from "./InstrumentTag";

function CodebookDialog({ tables, initialTable, onClose }) {
  const dialog = useRef(null);
  const searchInput = useRef(null);
  const titleId = useId();
  const [search, setSearch] = useState("");
  const [table, setTable] = useState(initialTable);
  const [instrument, setInstrument] = useState("");
  const labels = tables.flatMap((t) =>
    t.columns
      .filter((c) => c.label_name)
      .map((c) => ({
        ...c,
        table: t.name,
        ref: `${t.name}.${c.name}`,
        description:
          c.label_description === undefined
            ? c.description
            : c.label_description,
      })),
  );
  const instruments = [
    ...new Set(labels.filter((c) => c.instrument).map((c) => c.instrument)),
  ].sort((a, b) => a.localeCompare(b));
  const rows = labels
    .filter(
      (c) =>
        (!table || c.table === table) &&
        (!instrument ||
          (instrument === "unassigned"
            ? !c.instrument
            : c.instrument === instrument.slice(11))) &&
        [c.label_name, c.ref, c.instrument, c.label_level, c.description].some(
          (value) => value?.toLowerCase().includes(search.toLowerCase()),
        ),
    )
    .sort(
      (a, b) =>
        a.label_name.localeCompare(b.label_name) || a.ref.localeCompare(b.ref),
    );

  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    element.showModal();
    searchInput.current.focus();
    document.body.style.overflow = "hidden";
    return () => {
      element.close();
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);

  return (
    <dialog
      ref={dialog}
      className="data-exports__codebook"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <div className="data-exports__header">
        <h2 id={titleId}>Label codebook</h2>
        <button type="button" onClick={onClose} aria-label="Close codebook">
          Close ×
        </button>
      </div>
      <p className="data-exports__muted">
        Descriptions from the annotation label definitions. Definitions are
        shared across datasets.
      </p>
      <div className="data-exports__actions">
        <input
          ref={searchInput}
          type="search"
          aria-label="Search codebook"
          placeholder="Search labels or descriptions…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <label>
          Table{" "}
          <select
            aria-label="Codebook table"
            value={table}
            onChange={(event) => setTable(event.target.value)}
          >
            <option value="">All tables</option>
            {tables.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Instrument{" "}
          <select
            aria-label="Codebook instrument"
            value={instrument}
            onChange={(event) => setInstrument(event.target.value)}
          >
            <option value="">All instruments</option>
            <option value="unassigned">Unassigned labels</option>
            {instruments.map((name) => (
              <option key={name} value={`instrument:${name}`}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="data-exports__muted" role="status">
        {rows.length} labels
      </p>
      <div className="data-exports__codebook-results">
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>Label / column</th>
                <th>Instrument</th>
                <th>Level / type</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.ref}>
                  <td>
                    <strong>{c.label_name}</strong>
                    <small>{c.ref}</small>
                  </td>
                  <td>
                    <InstrumentTag name={c.instrument} />
                  </td>
                  <td>
                    {c.label_level}
                    <small>{c.label_datatype || c.type}</small>
                  </td>
                  <td className="data-exports__codebook-description">
                    {c.description?.trim() || "No description provided."}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p>No labels match these filters.</p>
        )}
      </div>
    </dialog>
  );
}

CodebookDialog.propTypes = {
  tables: PropTypes.array.isRequired,
  initialTable: PropTypes.string.isRequired,
  onClose: PropTypes.func.isRequired,
};

export default function Codebook({ tables, initialTable }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="btn-outline data-exports__codebook-link"
        aria-haspopup="dialog"
        disabled={!tables.length}
        onClick={() => setOpen(true)}
      >
        Codebook
      </button>
      {open && (
        <CodebookDialog
          tables={tables}
          initialTable={initialTable}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

Codebook.propTypes = {
  tables: PropTypes.array.isRequired,
  initialTable: PropTypes.string.isRequired,
};
