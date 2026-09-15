import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../../context/AuthContext";
import { request } from "./api";
import Conditions from "./Conditions";
import ExportHistory from "./ExportHistory";
import InstrumentTag from "./InstrumentTag";
import Codebook from "./Codebook";
import TopBar from "../../components/TopBar";
import "./DataExplorer.css";

const blank = (table = "") => ({
  table,
  joins: [],
  columns: [],
  filters: { op: "and", rules: [] },
  sort: [],
});
export default function DataExplorer() {
  const { isAdmin, isStaff, loading } = useAuth();
  const canExport = isAdmin || isStaff;
  const allDatasets = isAdmin ? "All datasets" : "All permitted datasets";
  const [catalog, setCatalog] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [dataset, setDataset] = useState(null);
  const [retentionHours, setRetentionHours] = useState(24);
  const [relationships, setRelationships] = useState([]);
  const [builder, setBuilder] = useState(blank());
  const [mode, setMode] = useState("builder");
  const [sql, setSql] = useState("");
  const [search, setSearch] = useState("");
  const [columnSearch, setColumnSearch] = useState("");
  const [instrumentFilter, setInstrumentFilter] = useState("all");
  const [preview, setPreview] = useState(null);
  const [reports, setReports] = useState([]);
  const [reportId, setReportId] = useState("");
  const [reportName, setReportName] = useState("");
  const [editingExport, setEditingExport] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("query");
  const revision = useRef(0);
  const refreshRevision = useRef(0);
  const draggedColumn = useRef(null);
  const [dropTarget, setDropTarget] = useState(null);
  const config = { mode, builder, sql, dataset };
  const activeTables = [builder.table, ...builder.joins];
  const columns = catalog
    .filter((t) => activeTables.includes(t.name))
    .flatMap((t) =>
      t.columns.map((c) => ({ ...c, ref: `${t.name}.${c.name}` })),
    );
  const columnByRef = new Map(columns.map((column) => [column.ref, column]));
  const instruments = [
    ...new Set(
      columns
        .filter((c) => c.label_name && c.instrument)
        .map((c) => c.instrument),
    ),
  ].sort((a, b) => a.localeCompare(b));
  const visibleColumns = columns.filter((c) => {
    const matchesInstrument =
      instrumentFilter === "all" ||
      (instrumentFilter === "metadata" && !c.label_name) ||
      (instrumentFilter === "unassigned" && c.label_name && !c.instrument) ||
      (instrumentFilter.startsWith("instrument:") &&
        c.label_name &&
        c.instrument === instrumentFilter.slice(11));
    return (
      matchesInstrument &&
      [c.ref, c.label_name, c.instrument]
        .filter(Boolean)
        .some((text) => text.toLowerCase().includes(columnSearch.toLowerCase()))
    );
  });
  const visibleRefs = new Set(visibleColumns.map((c) => c.ref));
  const combinedSelection = [...new Set([...builder.columns, ...visibleRefs])];
  const related = catalog.filter(
    (t) =>
      !activeTables.includes(t.name) &&
      relationships.some(
        ([left, , right]) =>
          (activeTables.includes(left) && right === t.name) ||
          (activeTables.includes(right) && left === t.name),
      ),
  );
  const refresh = useCallback(
    async (background = false) => {
      const version = ++refreshRevision.current;
      const [r, j] = await Promise.all([
        request("/reports", "GET", undefined, background),
        request(
          `/exports?offset=${historyOffset}${dataset ? `&dataset=${encodeURIComponent(dataset)}` : ""}`,
          "GET",
          undefined,
          background,
        ),
      ]);
      if (version === refreshRevision.current) {
        setReports(r);
        setJobs(j);
      }
    },
    [historyOffset, dataset],
  );
  useEffect(() => {
    if (!canExport) return;
    let alive = true;
    request("/catalog")
      .then((data) => {
        if (!alive) return;
        setCatalog(data.tables);
        setDatasets(data.datasets || []);
        setRetentionHours(data.limits?.retention_hours ?? 24);
        setRelationships(data.relationships);
        const first =
          data.tables.find((t) => t.name === "patient_labelled") ||
          data.tables[0];
        if (first)
          setBuilder({
            ...blank(first.name),
            columns: first.columns
              .filter((c) => !c.label_name)
              .slice(0, 8)
              .map((c) => `${first.name}.${c.name}`),
          });
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, [canExport]);
  useEffect(() => {
    if (canExport) refresh().catch((e) => setError(e.message));
  }, [canExport, refresh]);
  useEffect(() => {
    if (
      !canExport ||
      !jobs.some((j) => ["queued", "running"].includes(j.status))
    )
      return;
    const timer = setInterval(
      () => refresh(true).catch((e) => setError(e.message)),
      5000,
    );
    return () => clearInterval(timer);
  }, [canExport, jobs, refresh]);

  const run = async (action) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const change = (next) => {
    if (
      next.table !== builder.table ||
      next.joins.join() !== builder.joins.join()
    )
      setInstrumentFilter("all");
    revision.current += 1;
    setBuilder(next);
    setPreview(null);
  };
  const load = (c) => {
    draggedColumn.current = null;
    setDropTarget(null);
    setInstrumentFilter("all");
    revision.current += 1;
    setMode(c.mode);
    setDataset(c.dataset || null);
    setHistoryOffset(0);
    setBuilder(c.builder || blank());
    setSql(c.sql || "");
    setPreview(null);
    setTab("query");
    setDetail(null);
  };
  const showPreview = (offset = 0) =>
    run(async () => {
      const version = revision.current;
      const data = await request(`/preview?offset=${offset}`, "POST", config);
      if (version === revision.current) setPreview(data);
    });
  const exportFile = (format) =>
    run(async () => {
      if (!reportName.trim()) throw new Error("Export name is required.");
      await request("/exports", "POST", {
        ...config,
        format,
        name: reportName.trim(),
        source_export_id: editingExport?.id || null,
      });
      setEditingExport(null);
      setNotice(
        "Export queued. You can continue working and download it from Export history when ready.",
      );
      setHistoryOffset(0);
      await refresh();
      setTab("history");
    });
  const save = (asNew) =>
    run(async () => {
      const row = await request(
        reportId && !asNew ? `/reports/${reportId}` : "/reports",
        reportId && !asNew ? "PUT" : "POST",
        { name: reportName, configuration: config },
      );
      setReportId(row.id);
      await refresh();
      setNotice("Report saved.");
    });
  const reorder = (index, delta) => {
    const selected = [...builder.columns];
    [selected[index], selected[index + delta]] = [
      selected[index + delta],
      selected[index],
    ];
    change({ ...builder, columns: selected });
  };
  if (loading) return null;
  if (!canExport) return <Navigate to="/" replace />;
  return (
    <div className="explorer">
      <TopBar />
      <main className="explorer__main">
        <header className="explorer__header">
          <div>
            <h1>Data Exports</h1>
            <p>
              Browse research tables and build reusable exports. Research data
              is read-only.
            </p>
          </div>
          <div className="explorer__actions">
            <Codebook tables={catalog} initialTable={builder.table} />
            <span className="explorer__badge">
              {isAdmin ? "Admin" : "Staff"} · Read only
            </span>
          </div>
        </header>
        <nav className="explorer__tabs" aria-label="Data Exports sections">
          <button
            className={tab === "query" ? "active" : ""}
            onClick={() => setTab("query")}
          >
            Build export
          </button>
          <button
            className={tab === "history" ? "active" : ""}
            onClick={() => setTab("history")}
          >
            Export history
          </button>
        </nav>
        <section className="explorer__dataset-scope" aria-label="Dataset scope">
          <label>
            Dataset
            <select
              aria-label="Dataset"
              value={dataset || ""}
              disabled={busy}
              onChange={(event) => {
                revision.current += 1;
                setDataset(event.target.value || null);
                setPreview(null);
                setHistoryOffset(0);
                setDetail(null);
              }}
            >
              <option value="">{allDatasets}</option>
              {dataset && !datasets.includes(dataset) && (
                <option value={dataset}>
                  {dataset} (not currently listed)
                </option>
              )}
              {datasets.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <div>
            <strong>Viewing: {dataset || allDatasets}</strong>
            <p>
              Applies to value choices, previews and exports in both builder and
              SQL modes. History shows exports created for the selected dataset.
            </p>
          </div>
        </section>
        {error && (
          <div className="explorer__error" role="alert">
            {error}
          </div>
        )}
        {notice && (
          <div className="explorer__notice" role="status">
            {notice}
          </div>
        )}
        {tab === "query" ? (
          <>
            <section className="explorer__reports">
              <label>
                {isAdmin ? "Saved reports" : "My reports"}
                <select
                  aria-label={isAdmin ? "Saved reports" : "My reports"}
                  value={reportId}
                  onChange={(e) => {
                    const report = reports.find((r) => r.id === e.target.value);
                    setReportId(e.target.value);
                    setEditingExport(null);
                    setReportName(report?.name || "");
                    if (report) load(report.configuration);
                  }}
                >
                  <option value="">Unsaved report</option>
                  {reports.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name} · {r.updated_by}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Export name
                <input
                  required
                  aria-describedby="export-name-hint"
                  value={reportName}
                  onChange={(e) => setReportName(e.target.value)}
                  maxLength={120}
                />
              </label>
              <button
                className="btn-outline"
                disabled={busy || !reportName.trim()}
                onClick={() => save(false)}
              >
                {reportId ? "Update report" : "Save report"}
              </button>
              {reportId && (
                <>
                  <button
                    className="btn-outline"
                    disabled={busy || !reportName.trim()}
                    onClick={() => save(true)}
                  >
                    Save a copy
                  </button>
                  <button
                    className="btn-outline"
                    disabled={busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          "Delete this report? Export history will be retained.",
                        )
                      )
                        run(async () => {
                          await request(`/reports/${reportId}`, "DELETE");
                          setReportId("");
                          setReportName("");
                          await refresh();
                        });
                    }}
                  >
                    Delete report
                  </button>
                </>
              )}
              <p id="export-name-hint" className="explorer__muted">
                Required for CSV and Excel exports. Also used when saving a
                report.
              </p>
            </section>
            {editingExport && (
              <div className="explorer__notice" role="status">
                Editing export: <strong>{editingExport.name}</strong>. Change
                the name, dataset, columns or conditions, then export again to
                create a new history entry. The original export stays available.
              </div>
            )}
            <div className="explorer__layout">
              <aside className="explorer__sidebar">
                <h2>Research tables</h2>
                <input
                  aria-label="Search tables"
                  placeholder="Search tables…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                {catalog
                  .filter((t) => t.name.includes(search.toLowerCase()))
                  .map((t) => (
                    <button
                      disabled={mode === "sql"}
                      className={`explorer__table-choice ${builder.table === t.name ? "active" : ""}`}
                      key={t.name}
                      onClick={() =>
                        change({
                          ...blank(t.name),
                          columns: t.columns
                            .filter((c) => !c.label_name)
                            .slice(0, 8)
                            .map((c) => `${t.name}.${c.name}`),
                        })
                      }
                    >
                      <strong>{t.name}</strong>
                      <small>{t.description}</small>
                    </button>
                  ))}
                {!catalog.length && <p>No research tables available.</p>}
              </aside>
              <section className="explorer__workspace">
                <div className="explorer__actions">
                  <button
                    className={mode === "builder" ? "active" : "btn-outline"}
                    onClick={() => {
                      revision.current += 1;
                      setMode("builder");
                      setPreview(null);
                    }}
                  >
                    Visual builder
                  </button>
                  <button
                    className={mode === "sql" ? "active" : "btn-outline"}
                    onClick={() => {
                      if (preview) setSql(preview.base_sql || preview.sql);
                      revision.current += 1;
                      setMode("sql");
                      setPreview(null);
                    }}
                  >
                    SQL query
                  </button>
                </div>
                {mode === "builder" ? (
                  <>
                    <h2>{builder.table || "Choose a table"}</h2>
                    <p>
                      Start with rows from this table. Related tables use left
                      joins: unmatched rows remain, and multiple matches produce
                      additional rows.
                    </p>
                    <div className="explorer__actions">
                      <label>
                        Add related table
                        <select
                          aria-label="Add related table"
                          value=""
                          onChange={(e) => {
                            if (e.target.value)
                              change({
                                ...builder,
                                joins: [...builder.joins, e.target.value],
                              });
                          }}
                        >
                          <option value="">Choose a relationship…</option>
                          {related.map((t) => (
                            <option key={t.name}>{t.name}</option>
                          ))}
                        </select>
                      </label>
                      {builder.joins.length > 0 && (
                        <button
                          className="btn-outline"
                          onClick={() =>
                            change({
                              ...builder,
                              joins: [],
                              columns: builder.columns.filter((c) =>
                                c.startsWith(builder.table + "."),
                              ),
                              filters: { op: "and", rules: [] },
                              sort: [],
                            })
                          }
                        >
                          Clear relationships and filters
                        </button>
                      )}
                    </div>
                    {builder.joins.length > 0 && (
                      <p>Joined tables: {builder.joins.join(" → ")}</p>
                    )}
                    <details open>
                      <summary>
                        Columns ({builder.columns.length} selected)
                      </summary>
                      <div className="explorer__actions">
                        <input
                          aria-label="Search columns"
                          placeholder="Search columns or labels…"
                          value={columnSearch}
                          onChange={(e) => setColumnSearch(e.target.value)}
                        />
                        <label>
                          Instrument{" "}
                          <select
                            aria-label="Column instrument"
                            value={instrumentFilter}
                            onChange={(e) =>
                              setInstrumentFilter(e.target.value)
                            }
                          >
                            <option value="all">All columns</option>
                            <option value="metadata">Table metadata</option>
                            {instruments.map((name) => (
                              <option key={name} value={`instrument:${name}`}>
                                {name}
                              </option>
                            ))}
                            <option value="unassigned">
                              Unassigned labels
                            </option>
                          </select>
                        </label>
                        <button
                          className="btn-outline"
                          disabled={
                            !visibleColumns.length ||
                            combinedSelection.length > 500
                          }
                          title="Select the shown columns; maximum 500 total"
                          onClick={() =>
                            change({ ...builder, columns: combinedSelection })
                          }
                        >
                          Select shown ({visibleColumns.length})
                        </button>
                        <button
                          className="btn-outline"
                          disabled={
                            !builder.columns.some((ref) => visibleRefs.has(ref))
                          }
                          onClick={() =>
                            change({
                              ...builder,
                              columns: builder.columns.filter(
                                (ref) => !visibleRefs.has(ref),
                              ),
                            })
                          }
                        >
                          Deselect shown
                        </button>
                      </div>
                      <p className="explorer__muted">
                        Filter by instrument to select its labels together.
                        Existing selections remain when you change the filter.
                        Instrument groups apply to label columns in labelled
                        tables.
                      </p>
                      <p className="explorer__muted">
                        Drag anywhere on a row in the right-hand list to reorder
                        selected columns, or use the arrow buttons. Use × to
                        remove a column.
                      </p>
                      <div className="explorer__column-layout">
                        <div className="explorer__columns">
                          {!visibleColumns.length && (
                            <p>No columns match this instrument and search.</p>
                          )}
                          {visibleColumns.map((c) => (
                            <label
                              key={c.ref}
                              title={[
                                c.ref,
                                c.label_name,
                                c.instrument,
                                c.description,
                              ]
                                .filter(Boolean)
                                .join("\n")}
                            >
                              <input
                                type="checkbox"
                                checked={builder.columns.includes(c.ref)}
                                onChange={(e) =>
                                  change({
                                    ...builder,
                                    columns: e.target.checked
                                      ? [...builder.columns, c.ref]
                                      : builder.columns.filter(
                                          (v) => v !== c.ref,
                                        ),
                                  })
                                }
                              />
                              <span className="explorer__column-text">
                                <span className="explorer__column-name">
                                  {c.ref}
                                </span>
                                <span className="explorer__column-type">
                                  {c.type}
                                </span>
                                {c.label_name && (
                                  <InstrumentTag name={c.instrument} />
                                )}
                              </span>
                            </label>
                          ))}
                        </div>
                        <ol
                          className="explorer__selected"
                          aria-label="Selected column order"
                        >
                          {builder.columns.map((c, i) => (
                            <li
                              key={c}
                              data-column-ref={c}
                              data-drop-target={dropTarget === c}
                              draggable
                              onDragStart={(event) => {
                                draggedColumn.current = c;
                                event.dataTransfer.effectAllowed = "move";
                                event.dataTransfer.setData("text/plain", c);
                              }}
                              onDragEnd={() => {
                                draggedColumn.current = null;
                                setDropTarget(null);
                              }}
                              onDragOver={(event) => {
                                if (!draggedColumn.current) return;
                                event.preventDefault();
                                event.dataTransfer.dropEffect = "move";
                                setDropTarget(c);
                              }}
                              onDrop={(event) => {
                                event.preventDefault();
                                const source = builder.columns.indexOf(
                                  draggedColumn.current,
                                );
                                if (source >= 0 && source !== i) {
                                  const selected = [...builder.columns];
                                  const [moved] = selected.splice(source, 1);
                                  selected.splice(i, 0, moved);
                                  change({ ...builder, columns: selected });
                                }
                                draggedColumn.current = null;
                                setDropTarget(null);
                              }}
                            >
                              <span
                                className="explorer__drag-handle"
                                aria-hidden="true"
                              >
                                ⠿
                              </span>
                              <span className="explorer__column-text" title={c}>
                                <span className="explorer__column-name">
                                  {c}
                                </span>
                                <span className="explorer__column-type">
                                  {columnByRef.get(c)?.type}
                                </span>
                                {columnByRef.get(c)?.label_name && (
                                  <InstrumentTag
                                    name={columnByRef.get(c).instrument}
                                  />
                                )}
                              </span>
                              <button
                                aria-label={`Move ${c} up`}
                                disabled={i === 0}
                                onClick={() => reorder(i, -1)}
                              >
                                ↑
                              </button>
                              <button
                                aria-label={`Move ${c} down`}
                                disabled={i === builder.columns.length - 1}
                                onClick={() => reorder(i, 1)}
                              >
                                ↓
                              </button>
                              <button
                                aria-label={`Remove ${c}`}
                                title="Remove column"
                                onClick={() =>
                                  change({
                                    ...builder,
                                    columns: builder.columns.filter(
                                      (column) => column !== c,
                                    ),
                                  })
                                }
                              >
                                ×
                              </button>
                            </li>
                          ))}
                        </ol>
                      </div>
                    </details>
                    <Conditions
                      group={builder.filters}
                      columns={columns}
                      dataset={dataset}
                      onChange={(filters) => change({ ...builder, filters })}
                    />
                    <details>
                      <summary>Sorting ({builder.sort.length})</summary>
                      {builder.sort.map((s, i) => (
                        <div className="explorer__actions" key={i}>
                          <select
                            aria-label="Sort column"
                            value={s.column}
                            onChange={(e) =>
                              change({
                                ...builder,
                                sort: builder.sort.map((item, j) =>
                                  i === j
                                    ? { ...item, column: e.target.value }
                                    : item,
                                ),
                              })
                            }
                          >
                            {columns.map((c) => (
                              <option key={c.ref}>{c.ref}</option>
                            ))}
                          </select>
                          <select
                            aria-label="Sort direction"
                            value={s.direction}
                            onChange={(e) =>
                              change({
                                ...builder,
                                sort: builder.sort.map((item, j) =>
                                  i === j
                                    ? { ...item, direction: e.target.value }
                                    : item,
                                ),
                              })
                            }
                          >
                            <option value="asc">Ascending</option>
                            <option value="desc">Descending</option>
                          </select>
                          <button
                            onClick={() =>
                              change({
                                ...builder,
                                sort: builder.sort.filter((_, j) => j !== i),
                              })
                            }
                          >
                            Remove sort
                          </button>
                        </div>
                      ))}
                      <button
                        className="btn-outline"
                        disabled={!columns.length}
                        onClick={() =>
                          change({
                            ...builder,
                            sort: [
                              ...builder.sort,
                              { column: columns[0].ref, direction: "asc" },
                            ],
                          })
                        }
                      >
                        Add sort
                      </button>
                    </details>
                  </>
                ) : (
                  <>
                    <h2>Read-only SQL</h2>
                    <p>
                      One SELECT query over catalog tables. Joins, non-recursive
                      CTEs and common aggregates are supported. Advanced
                      functions and administrative commands are restricted.
                    </p>
                    <textarea
                      className="explorer__sql"
                      aria-label="SQL query"
                      spellCheck={false}
                      value={sql}
                      placeholder="SELECT * FROM patient_labelled"
                      onChange={(e) => {
                        revision.current += 1;
                        setSql(e.target.value);
                        setPreview(null);
                      }}
                    />
                    <p>
                      Returning to the visual builder restores its last
                      settings; SQL edits are kept separately.
                    </p>
                  </>
                )}
                <div className="explorer__actions">
                  <button disabled={busy} onClick={() => showPreview()}>
                    Preview results
                  </button>
                  <button
                    className="btn-outline"
                    disabled={busy || !reportName.trim()}
                    onClick={() => exportFile("csv")}
                  >
                    Export CSV
                  </button>
                  <button
                    className="btn-outline"
                    disabled={busy || !reportName.trim()}
                    onClick={() => exportFile("xlsx")}
                  >
                    Export Excel
                  </button>
                  {busy && <span role="status">Working…</span>}
                </div>
                <p className="explorer__muted">
                  Preview shows 200 rows per page. Exports include all matching
                  rows within the configured limits. Files are available for{" "}
                  {retentionHours}
                  hours; saved reports rerun against current data.
                </p>
                {preview && (
                  <>
                    <details>
                      <summary>Equivalent SQL</summary>
                      <pre className="explorer__sql-output">{preview.sql}</pre>
                      <button
                        className="btn-outline"
                        onClick={() =>
                          run(async () => {
                            await navigator.clipboard.writeText(preview.sql);
                            setNotice("SQL copied.");
                          })
                        }
                      >
                        Copy SQL
                      </button>
                    </details>
                    <div className="explorer__results">
                      <table>
                        <thead>
                          <tr>
                            {preview.columns.map((c, i) => (
                              <th key={i}>{c}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {preview.rows.map((r, i) => (
                            <tr key={i}>
                              {r.map((v, j) => (
                                <td key={j}>
                                  {v === null ? (
                                    <span className="explorer__muted">
                                      NULL
                                    </span>
                                  ) : (
                                    v
                                  )}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {preview.rows.length === 0 && <p>No matching rows.</p>}
                    <div className="explorer__actions">
                      <button
                        className="btn-outline"
                        disabled={busy || preview.offset === 0}
                        onClick={() =>
                          showPreview(Math.max(0, preview.offset - 200))
                        }
                      >
                        Previous rows
                      </button>
                      <span>
                        Rows {preview.rows.length ? preview.offset + 1 : 0}–
                        {preview.offset + preview.rows.length}
                      </span>
                      <button
                        className="btn-outline"
                        disabled={busy || !preview.has_more}
                        onClick={() => showPreview(preview.offset + 200)}
                      >
                        Next rows
                      </button>
                    </div>
                  </>
                )}
              </section>
            </div>
          </>
        ) : (
          <ExportHistory
            jobs={jobs}
            detail={detail}
            busy={busy}
            historyOffset={historyOffset}
            onPage={setHistoryOffset}
            onRefresh={() => run(refresh)}
            onDetails={(id) =>
              run(async () => setDetail(await request(`/exports/${id}`)))
            }
            onCloseDetails={() => setDetail(null)}
            onEdit={(job) => {
              load(job.configuration);
              setReportId("");
              setReportName(job.name);
              setEditingExport({ id: job.id, name: job.name });
              setNotice("");
              setError("");
            }}
            onCancel={(id) =>
              run(async () => {
                await request(`/exports/${id}/cancel`, "POST");
                await refresh();
              })
            }
          />
        )}
      </main>
    </div>
  );
}
