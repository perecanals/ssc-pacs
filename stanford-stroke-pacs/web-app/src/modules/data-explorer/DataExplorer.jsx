import { useCallback, useEffect, useRef, useState } from "react";
import PropTypes from "prop-types";
import { Navigate } from "react-router-dom";
import { useAuth } from "../../context/AuthContext";
import { apiFetch } from "../../api/client";
import TopBar from "../../components/TopBar";
import "./DataExplorer.css";

const ROOT = "/api/data-explorer";
const blank = (table = "") => ({
  table,
  joins: [],
  columns: [],
  filters: { op: "and", rules: [] },
  sort: [],
});
const operators = {
  eq: "equals",
  ne: "does not equal",
  contains: "contains",
  lt: "less than",
  le: "at most",
  gt: "greater than",
  ge: "at least",
  is_null: "is empty (NULL)",
  not_null: "is not empty",
};
async function request(path, method = "GET", body, background = false) {
  const res = await apiFetch(ROOT + path, {
    method,
    trackActivity: !background,
    ...(background ? { headers: { "X-Explorer-Poll": "1" } } : {}),
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await res.json();
  if (!res.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Please check the query settings.",
    );
  return data;
}

function Filters({ group, columns, onChange, depth = 0 }) {
  const update = (index, value) =>
    onChange({
      ...group,
      rules: group.rules.map((r, i) => (i === index ? value : r)),
    });
  return (
    <fieldset className="explorer__filters">
      <legend>
        Match{" "}
        <select
          aria-label="Filter combination"
          value={group.op}
          onChange={(e) => onChange({ ...group, op: e.target.value })}
        >
          <option value="and">all conditions</option>
          <option value="or">any condition</option>
        </select>
      </legend>
      {group.rules.map((rule, i) => (
        <div className="explorer__filter" key={i}>
          {rule.rules ? (
            <Filters
              group={rule}
              columns={columns}
              depth={depth + 1}
              onChange={(r) => update(i, r)}
            />
          ) : (
            <>
              <select
                aria-label="Filter column"
                value={rule.column}
                onChange={(e) => update(i, { ...rule, column: e.target.value })}
              >
                {columns.map((c) => (
                  <option key={c.ref}>{c.ref}</option>
                ))}
              </select>
              <select
                aria-label="Filter operator"
                value={rule.op}
                onChange={(e) => update(i, { ...rule, op: e.target.value })}
              >
                {Object.entries(operators).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
              {!["is_null", "not_null"].includes(rule.op) && (
                <input
                  aria-label="Filter value"
                  placeholder={
                    columns.find((c) => c.ref === rule.column)?.type || "Value"
                  }
                  value={rule.value ?? ""}
                  onChange={(e) =>
                    update(i, { ...rule, value: e.target.value })
                  }
                />
              )}
            </>
          )}
          <button
            className="btn-outline"
            aria-label="Remove condition"
            onClick={() =>
              onChange({
                ...group,
                rules: group.rules.filter((_, j) => i !== j),
              })
            }
          >
            Remove
          </button>
        </div>
      ))}
      <button
        className="btn-outline"
        disabled={!columns.length}
        onClick={() =>
          onChange({
            ...group,
            rules: [
              ...group.rules,
              { column: columns[0].ref, op: "eq", value: "" },
            ],
          })
        }
      >
        Add condition
      </button>{" "}
      {depth < 3 && (
        <button
          className="btn-outline"
          onClick={() =>
            onChange({
              ...group,
              rules: [...group.rules, { op: "or", rules: [] }],
            })
          }
        >
          Add group
        </button>
      )}
    </fieldset>
  );
}
Filters.propTypes = {
  group: PropTypes.object.isRequired,
  columns: PropTypes.array.isRequired,
  onChange: PropTypes.func.isRequired,
  depth: PropTypes.number,
};

export default function DataExplorer() {
  const { isAdmin, loading } = useAuth();
  const [catalog, setCatalog] = useState([]);
  const [retentionHours, setRetentionHours] = useState(24);
  const [relationships, setRelationships] = useState([]);
  const [builder, setBuilder] = useState(blank());
  const [mode, setMode] = useState("builder");
  const [sql, setSql] = useState("");
  const [search, setSearch] = useState("");
  const [columnSearch, setColumnSearch] = useState("");
  const [preview, setPreview] = useState(null);
  const [reports, setReports] = useState([]);
  const [reportId, setReportId] = useState("");
  const [reportName, setReportName] = useState("");
  const [jobs, setJobs] = useState([]);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("query");
  const revision = useRef(0);
  const config = { mode, builder, sql };
  const activeTables = [builder.table, ...builder.joins];
  const columns = catalog
    .filter((t) => activeTables.includes(t.name))
    .flatMap((t) =>
      t.columns.map((c) => ({ ...c, ref: `${t.name}.${c.name}` })),
    );
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
      const [r, j] = await Promise.all([
        request("/reports", "GET", undefined, background),
        request(
          `/exports?offset=${historyOffset}`,
          "GET",
          undefined,
          background,
        ),
      ]);
      setReports(r);
      setJobs(j);
    },
    [historyOffset],
  );
  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    request("/catalog")
      .then((data) => {
        if (!alive) return;
        setCatalog(data.tables);
        setRetentionHours(data.limits?.retention_hours ?? 24);
        setRelationships(data.relationships);
        const first =
          data.tables.find((t) => t.name === "patient_labelled") ||
          data.tables[0];
        if (first)
          setBuilder({
            ...blank(first.name),
            columns: first.columns
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
  }, [isAdmin]);
  useEffect(() => {
    if (isAdmin) refresh().catch((e) => setError(e.message));
  }, [isAdmin, refresh]);
  useEffect(() => {
    if (!isAdmin || !jobs.some((j) => ["queued", "running"].includes(j.status)))
      return;
    const timer = setInterval(
      () => refresh(true).catch((e) => setError(e.message)),
      5000,
    );
    return () => clearInterval(timer);
  }, [isAdmin, jobs, refresh]);

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
    revision.current += 1;
    setBuilder(next);
    setPreview(null);
  };
  const load = (c) => {
    revision.current += 1;
    setMode(c.mode);
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
      await request("/exports", "POST", { ...config, format });
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
      setNotice("Shared report saved.");
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
  if (!isAdmin) return <Navigate to="/" replace />;
  return (
    <div className="explorer">
      <TopBar />
      <main className="explorer__main">
        <header className="explorer__header">
          <div>
            <h1>Data Explorer</h1>
            <p>
              Browse research tables and build reusable exports. Research data
              is read-only.
            </p>
          </div>
          <span className="explorer__badge">Admin · Read only</span>
        </header>
        <nav className="explorer__tabs" aria-label="Data Explorer sections">
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
                Shared reports
                <select
                  aria-label="Shared reports"
                  value={reportId}
                  onChange={(e) => {
                    const report = reports.find((r) => r.id === e.target.value);
                    setReportId(e.target.value);
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
                Report name
                <input
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
                          "Delete this shared report? Export history will be retained.",
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
            </section>
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
                      if (preview) setSql(preview.sql);
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
                      <input
                        aria-label="Search columns"
                        placeholder="Search columns…"
                        value={columnSearch}
                        onChange={(e) => setColumnSearch(e.target.value)}
                      />
                      <div className="explorer__column-layout">
                        <div className="explorer__columns">
                          {columns
                            .filter((c) =>
                              c.ref
                                .toLowerCase()
                                .includes(columnSearch.toLowerCase()),
                            )
                            .map((c) => (
                              <label
                                key={c.ref}
                                title={c.description || c.type}
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
                                <span>
                                  {c.ref}
                                  <small>{c.type}</small>
                                </span>
                              </label>
                            ))}
                        </div>
                        <ol className="explorer__selected">
                          {builder.columns.map((c, i) => (
                            <li key={c}>
                              <span>{c}</span>
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
                            </li>
                          ))}
                        </ol>
                      </div>
                    </details>
                    <Filters
                      group={builder.filters}
                      columns={columns}
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
                    disabled={busy}
                    onClick={() => exportFile("csv")}
                  >
                    Export CSV
                  </button>
                  <button
                    className="btn-outline"
                    disabled={busy}
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
          <section className="explorer__history">
            <div className="explorer__actions">
              <h2>Export history</h2>
              <button
                className="btn-outline"
                disabled={busy}
                onClick={() => run(refresh)}
              >
                Refresh
              </button>
            </div>
            <p>
              Shared audit history records who exported, when, and the exact
              query configuration. A rerun reads current data.
            </p>
            <div className="explorer__results">
              <table>
                <thead>
                  <tr>
                    <th>Requested</th>
                    <th>User</th>
                    <th>Format</th>
                    <th>Status</th>
                    <th>Rows written</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.id}>
                      <td>{new Date(j.created_at).toLocaleString()}</td>
                      <td>{j.username}</td>
                      <td>{j.format.toUpperCase()}</td>
                      <td>
                        {j.status}
                        {j.error && <small>{j.error}</small>}
                      </td>
                      <td>{Number(j.row_count).toLocaleString()}</td>
                      <td>
                        <div className="explorer__actions">
                          <button
                            className="btn-outline"
                            disabled={busy}
                            onClick={() =>
                              run(async () =>
                                setDetail(await request(`/exports/${j.id}`)),
                              )
                            }
                          >
                            Details
                          </button>
                          <button
                            className="btn-outline"
                            onClick={() => {
                              load(j.configuration);
                              setReportId("");
                              setReportName("");
                            }}
                          >
                            Reuse configuration
                          </button>
                          {["running", "queued"].includes(j.status) && (
                            <button
                              className="btn-outline"
                              disabled={busy}
                              onClick={() =>
                                run(async () => {
                                  await request(
                                    `/exports/${j.id}/cancel`,
                                    "POST",
                                  );
                                  await refresh();
                                })
                              }
                            >
                              Cancel
                            </button>
                          )}
                          {j.status === "completed" && (
                            <a
                              className="btn-outline"
                              href={`${ROOT}/exports/${j.id}/download`}
                            >
                              Download
                            </a>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!jobs.length && <p>No exports yet.</p>}
            <div className="explorer__actions">
              <button
                className="btn-outline"
                disabled={!historyOffset}
                onClick={() =>
                  setHistoryOffset(Math.max(0, historyOffset - 50))
                }
              >
                Newer exports
              </button>
              <button
                className="btn-outline"
                disabled={jobs.length < 50}
                onClick={() => setHistoryOffset(historyOffset + 50)}
              >
                Older exports
              </button>
            </div>
            {detail && (
              <section className="explorer__detail">
                <div className="explorer__actions">
                  <h3>Export details</h3>
                  <button
                    className="btn-outline"
                    onClick={() => setDetail(null)}
                  >
                    Close details
                  </button>
                </div>
                <p>
                  {detail.username} · {detail.status} ·{" "}
                  {detail.file_size == null
                    ? "Size pending"
                    : `${Number(detail.file_size).toLocaleString()} bytes`}
                </p>
                <pre className="explorer__sql-output">
                  {detail.equivalent_sql}
                </pre>
                <details>
                  <summary>Recorded configuration</summary>
                  <pre>{JSON.stringify(detail.configuration, null, 2)}</pre>
                </details>
                <h4>Download requests</h4>
                {detail.downloads.map((d, i) => (
                  <p key={i}>
                    {d.username} · {new Date(d.requested_at).toLocaleString()}
                  </p>
                ))}
                {!detail.downloads.length && (
                  <p>No download requests recorded.</p>
                )}
              </section>
            )}
          </section>
        )}
      </main>
    </div>
  );
}
