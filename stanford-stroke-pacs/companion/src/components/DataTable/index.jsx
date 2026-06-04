import { useState, useEffect, useCallback, useMemo, useRef, Fragment } from "react";
import { createPortal } from "react-dom";
import PropTypes from "prop-types";
import { apiGet } from "../../api/client";
import { downloadDicomZip, resolveOhifLink, refreshSnapshots, refreshLabelledTables } from "./actions";
import { useColumnPrefs } from "../ColumnSelector";
import ColumnSelector from "../ColumnSelector";
import InlineEdit from "../InlineEdit";
import LabelDefModal from "../LabelDefModal";
import { useAuth } from "../../context/AuthContext";
import {
  LEVEL_RANK,
  LEVEL_CONFIG,
  buildBuiltinColumnCatalog,
  buildPatientStudiesUrl,
  formatDatetime,
  normalizeSelectFilterValues,
  hasFilterValue,
} from "../../utils/table";
import useTableData from "./useTableData";
import usePreferencePersistence from "./usePreferencePersistence";
import useDragColumns from "./useDragColumns";
import TableHeader from "./TableHeader";
import ChildRows, { DownloadIcon } from "./ChildRows";
import "../DataTable.css";

function DataTableInner({
  level,
  filters,
  onResetSidebarFilters,
  onPreviewSelect,
  activeRowKey,
  toolbarPortalTarget,
  previewOpen,
  previewUrl,
  onPreviewClose,
  serverPrefs,
}) {
  const { currentUser, isAdmin } = useAuth();
  const config = LEVEL_CONFIG[level];

  const [showDefModal, setShowDefModal] = useState(false);
  const [editingLabel, setEditingLabel] = useState(null);
  const [refreshingSnapshots, setRefreshingSnapshots] = useState(false);
  const [refreshingLabelledTables, setRefreshingLabelledTables] = useState(false);

  const [sortBy, setSortBy] = useState(serverPrefs.sortBy || config.sortDefault);
  const [sortDir, setSortDir] = useState(serverPrefs.sortDir || "asc");
  const [columnFilters, setColumnFilters] = useState(
    serverPrefs.columnFilters && typeof serverPrefs.columnFilters === "object"
      ? serverPrefs.columnFilters
      : {},
  );
  const filterTimeout = useRef(null);
  const [frozenFirstCol, setFrozenFirstCol] = useState(!!serverPrefs.freezeFirstCol);
  const [fontScale, setFontScale] = useState(() => {
    const v = Number(serverPrefs.fontScale);
    return Number.isFinite(v) && v >= 0.85 && v <= 1.25 ? v : 1;
  });
  const adjustFontScale = (delta) => {
    setFontScale((prev) => {
      const next = Math.round((prev + delta) * 100) / 100;
      return Math.min(1.25, Math.max(0.85, next));
    });
  };

  const [expanded, setExpanded] = useState({});
  const [childRowsData, setChildRowsData] = useState({});
  const [grandExpanded, setGrandExpanded] = useState({});
  const [grandChildRows, setGrandChildRows] = useState({});

  const builtinCols = useMemo(() => buildBuiltinColumnCatalog(level), [level]);

  const [labelDefs, setLabelDefs] = useState([]);
  const fetchLabelDefs = useCallback(async () => {
    try { setLabelDefs(await apiGet("/api/label-definitions")); }
    catch { setLabelDefs([]); }
  }, []);
  useEffect(() => { fetchLabelDefs(); }, [fetchLabelDefs]);

  const {
    allCols, visibleCols, visibleKeys, columnOrder,
    toggle, setKeysVisible, reorder, resetColumns,
  } = useColumnPrefs(labelDefs, builtinCols, level, serverPrefs);

  // A sidebar label quick-filter enables that label's column the same way a
  // dropdown click would (one-time, persisted) — so the ColumnSelector
  // checkbox reflects it and the user can hide it again without clearing the
  // filter. Keyed only on the filter changing: a later manual hide is not
  // undone, and clearing the filter leaves the column as the user left it.
  const labelFilterKey = filters.label ? `label:${filters.label}` : null;
  useEffect(() => {
    if (labelFilterKey && !visibleKeys.includes(labelFilterKey)) {
      setKeysVisible([labelFilterKey], true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [labelFilterKey]);

  const { items, total, loading, hasMore, loadMore, reload, resetNonce } = useTableData({
    level, config, filters, sortBy, sortDir, columnFilters, allCols,
  });

  const scrollRef = useRef(null);
  const sentinelRef = useRef(null);

  // Scroll back to the top whenever the list is reset (filter/sort/level change).
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [resetNonce]);

  // Auto-load the next page when the bottom sentinel scrolls into view.
  // loadMore() self-guards on loading/hasMore so fast scroll can't stack
  // requests. The sentinel is only rendered when there are rows, so the
  // observer never fires on an empty or still-loading list.
  useEffect(() => {
    const root = scrollRef.current;
    const target = sentinelRef.current;
    if (!root || !target) return undefined;
    const io = new IntersectionObserver(
      (entries) => { if (entries[0].isIntersecting) loadMore(); },
      { root, rootMargin: "200px", threshold: 0 },
    );
    io.observe(target);
    return () => io.disconnect();
  }, [loadMore, items.length === 0, level]);

  const {
    dragColKey, dragOverKey, dropSide,
    handleDragStart, handleDragOver, handleDragLeave, handleDrop, handleDragEnd,
  } = useDragColumns(reorder);

  usePreferencePersistence({
    currentUser, level, visibleKeys, columnOrder, sortBy, sortDir, columnFilters, frozenFirstCol, fontScale,
  });

  const [downloadingSeries, setDownloadingSeries] = useState(null);

  useEffect(() => {
    if (level !== "patient") return;
    setChildRowsData({});
    setExpanded({});
  }, [level, filters.studyImportLabel]);

  const childConfig = config.expandable ? LEVEL_CONFIG[config.childLevel] : null;
  const grandChildConfig = childConfig?.expandable ? LEVEL_CONFIG[childConfig.childLevel] : null;

  const handleMutated = () => {
    reload();
    for (const [rowId, isExp] of Object.entries(expanded)) {
      if (!isExp) continue;
      const row = items.find((r) => r[config.idCol] === rowId);
      if (row && config.expandEndpoint) {
        const url = level === "patient"
          ? buildPatientStudiesUrl(row, filters.studyImportLabel)
          : config.expandEndpoint(row);
        apiGet(url)
          .then((data) => setChildRowsData((prev) => ({ ...prev, [rowId]: data })))
          .catch(() => {});
      }
    }
    if (childConfig?.expandEndpoint) {
      for (const [gcKey, isExp] of Object.entries(grandExpanded)) {
        if (!isExp) continue;
        const [parentId, childId] = gcKey.split("::");
        const parentChildren = childRowsData[parentId];
        const child = parentChildren?.find((c) => c[childConfig.idCol] === childId);
        if (child) {
          apiGet(childConfig.expandEndpoint(child))
            .then((data) => setGrandChildRows((prev) => ({ ...prev, [gcKey]: data })))
            .catch(() => {});
        }
      }
    }
    window.__refreshLabelSidebar?.();
  };

  const handleSort = (key) => {
    if (sortBy === key) {
      setSortDir((prev) => (prev === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(key);
      setSortDir("asc");
    }
  };

  const handleColumnFilter = (key, value) => {
    clearTimeout(filterTimeout.current);
    filterTimeout.current = setTimeout(() => {
      setColumnFilters((prev) => ({ ...prev, [key]: value || null }));
    }, 400);
  };

  const handleBoolFilter = (key) => {
    clearTimeout(filterTimeout.current);
    setColumnFilters((prev) => {
      const cur = prev[key];
      const next = cur === "true" ? "false" : cur === "false" ? null : "true";
      return { ...prev, [key]: next };
    });
  };

  const handleSelectFilterToggle = (key, option) => {
    clearTimeout(filterTimeout.current);
    setColumnFilters((prev) => {
      const current = normalizeSelectFilterValues(prev[key]);
      const next = current.includes(option)
        ? current.filter((value) => value !== option)
        : [...current, option];
      return { ...prev, [key]: next.length > 0 ? next : null };
    });
  };

  const handleSelectFilterClear = (key) => {
    clearTimeout(filterTimeout.current);
    setColumnFilters((prev) => ({ ...prev, [key]: null }));
  };

  const handleOhifLink = async (studyinstanceuid, seriesinstanceuid = null) => {
    try { await resolveOhifLink(studyinstanceuid, seriesinstanceuid); }
    catch (e) { alert(e?.message || "Could not resolve OHIF link"); }
  };

  const handleRefreshSnapshots = async () => {
    setRefreshingSnapshots(true);
    try {
      const data = await refreshSnapshots();
      alert(`Snapshots refreshed.\n${Object.entries(data.counts).map(([k, v]) => `${k}: ${v} rows`).join("\n")}`);
    } catch { alert("Failed to refresh snapshots"); }
    finally { setRefreshingSnapshots(false); }
  };

  const handleRefreshLabelledTables = async () => {
    setRefreshingLabelledTables(true);
    try {
      const data = await refreshLabelledTables();
      alert(`Labelled tables refreshed.\n${Object.entries(data.counts).map(([k, v]) => `${k}: ${v} rows`).join("\n")}`);
    } catch { alert("Failed to refresh labelled tables"); }
    finally { setRefreshingLabelledTables(false); }
  };

  const toggleExpand = async (rowId, row) => {
    if (expanded[rowId]) { setExpanded((p) => ({ ...p, [rowId]: false })); return; }
    setExpanded((p) => ({ ...p, [rowId]: true }));
    if (!childRowsData[rowId]) {
      const url = level === "patient" ? buildPatientStudiesUrl(row, filters.studyImportLabel) : config.expandEndpoint(row);
      try { const d = await apiGet(url); setChildRowsData((p) => ({ ...p, [rowId]: d })); }
      catch { setChildRowsData((p) => ({ ...p, [rowId]: [] })); }
    }
  };

  const toggleGrandExpand = async (key, childRow) => {
    if (grandExpanded[key]) { setGrandExpanded((p) => ({ ...p, [key]: false })); return; }
    setGrandExpanded((p) => ({ ...p, [key]: true }));
    if (!grandChildRows[key]) {
      try { const d = await apiGet(childConfig.expandEndpoint(childRow)); setGrandChildRows((p) => ({ ...p, [key]: d })); }
      catch { setGrandChildRows((p) => ({ ...p, [key]: [] })); }
    }
  };

  const handleDicomDownload = async (seriesinstanceuid) => {
    setDownloadingSeries(seriesinstanceuid);
    try { await downloadDicomZip(seriesinstanceuid); }
    catch (err) { alert(`Download failed: ${err.message}`); }
    finally { setDownloadingSeries(null); }
  };

  const allAnnotations = (row) => [...(row.annotations || []), ...(row.inherited_annotations || [])];

  const selectPreview = (row, srcLvl) => {
    if (!onPreviewSelect || !row?.studyinstanceuid) return;
    const isSeries = srcLvl === "series";
    onPreviewSelect({
      rowKey: isSeries ? `series:${row.seriesinstanceuid}` : `study:${row.studyinstanceuid}`,
      studyinstanceuid: row.studyinstanceuid,
      seriesinstanceuid: isSeries ? row.seriesinstanceuid || null : null,
      sourceLevel: srcLvl,
      patientId: row.patient_id || null,
      description: isSeries ? row.seriesdescription || null : row.studydescription || null,
    });
  };

  const renderCellValue = (row, col) => {
    if (col.builtin) {
      const raw = row[col.sourceKey] ?? "";
      if (col.sourceKey === "acquisitiondatetime") return formatDatetime(raw);
      return raw;
    }
    const labelName = col.key.replace("label:", "");
    return (
      <InlineEdit
        level={col.level || level}
        entity={row}
        labelName={labelName}
        datatype={col.datatype}
        defOptions={col.options || []}
        annotations={allAnnotations(row)}
        onMutated={handleMutated}
      />
    );
  };

  const renderActions = (row, rowLevel) => {
    const uid = row.studyinstanceuid;
    if (uid && (rowLevel === "study" || rowLevel === "series")) {
      return (
        <>
          <button onClick={() => handleOhifLink(uid, rowLevel === "series" ? row.seriesinstanceuid : null)} className="link-btn">OHIF</button>
          {rowLevel === "series" && row.seriesinstanceuid && isAdmin && (
            <button onClick={() => handleDicomDownload(row.seriesinstanceuid)} className="link-btn"
              title="Download DICOM as zip" disabled={downloadingSeries === row.seriesinstanceuid}>
              {downloadingSeries === row.seriesinstanceuid ? "\u2026" : <DownloadIcon />}
            </button>
          )}
        </>
      );
    }
    return null;
  };

  const colsForLevel = (targetLevel) => {
    const builtins = allCols.filter((c) => c.builtin && visibleKeys.includes(c.key) && c.level === targetLevel);
    const labels = visibleCols.filter((c) => !c.builtin && c.level === targetLevel);
    return [...builtins, ...labels];
  };
  const childCols = childConfig ? colsForLevel(config.childLevel) : [];
  const grandChildCols = grandChildConfig ? colsForLevel(childConfig.childLevel) : [];
  const mainTableCols = visibleCols.filter(
    (c) => (c.builtin ? c.level === level : LEVEL_RANK[c.level] <= LEVEL_RANK[level]),
  );
  const showActions = level !== "patient";
  const parentColSpan = mainTableCols.length + (config.expandable ? 1 : 0) + (showActions ? 1 : 0);
  const childIsExpandable = !!grandChildConfig;
  // +1 for the child table's trailing spacer column so the grandchild
  // wrapper/empty cell spans the full child-table width (not cut short).
  const gcColSpan = childCols.length + (childIsExpandable ? 2 : 1) + 1;

  const handleMainRowClick = (rowId, row) => {
    if (level === "study") {
      if (expanded[rowId]) { toggleExpand(rowId, row); return; }
      selectPreview(row, "study");
    } else if (level === "series") {
      selectPreview(row, "series");
    }
    if (config.expandable) toggleExpand(rowId, row);
  };

  const handleChildRowClick = (gcKey, child) => {
    if (config.childLevel === "study") {
      if (grandExpanded[gcKey]) { toggleGrandExpand(gcKey, child); return; }
      selectPreview(child, "study");
    } else if (config.childLevel === "series") {
      selectPreview(child, "series");
    }
    if (childIsExpandable) toggleGrandExpand(gcKey, child);
  };

  const handleGrandChildRowClick = (row) => { selectPreview(row, "series"); };

  const handleResetDefaults = () => {
    resetColumns();
    setSortBy(config.sortDefault);
    setSortDir("asc");
    setColumnFilters({});
    setFrozenFirstCol(false);
    setFontScale(1);
  };

  // Clears only filters — the per-column table filters here plus the
  // sidebar quick filters (via the Navigator callback). Distinct from
  // "Reset View", which also resets column visibility/order/sort/etc.
  const handleResetFilters = () => {
    setColumnFilters({});
    onResetSidebarFilters?.();
  };

  const handleEditLabel = (labelDef) => {
    if (!currentUser) { alert("Please log in to edit label types"); return; }
    setEditingLabel(labelDef);
  };

  const topBarControls = (
    <>
      <ColumnSelector
        allCols={allCols}
        visibleKeys={visibleKeys}
        onToggle={toggle}
        onSetKeysVisible={setKeysVisible}
        onEditLabel={handleEditLabel}
      />
      <button onClick={handleResetFilters} className="pill-btn">Reset Filters</button>
      <button onClick={handleResetDefaults} className="pill-btn">Reset View</button>
      <button onClick={() => {
        if (!currentUser) { alert("Please log in to create label types"); return; }
        setShowDefModal(true);
      }} className="pill-btn">+ New label</button>
    </>
  );

  const countLabel = total === 0
    ? `0 ${config.entityLabel}`
    : `${total.toLocaleString()} ${config.entityLabel}`;
  const fontScalePct = Math.round(fontScale * 100);

  return (
    <div className="dt__panel" style={{ "--dt-font-scale": fontScale }}>
      {toolbarPortalTarget ? createPortal(topBarControls, toolbarPortalTarget) : null}

      <div className="dt__scroll" ref={scrollRef}>
        <table className="dt">
          <TableHeader
            config={config}
            level={level}
            mainTableCols={mainTableCols}
            showActions={showActions}
            frozenFirstCol={frozenFirstCol}
            setFrozenFirstCol={setFrozenFirstCol}
            sortBy={sortBy}
            sortDir={sortDir}
            columnFilters={columnFilters}
            dragOverKey={dragOverKey}
            dropSide={dropSide}
            dragColKeyRef={dragColKey}
            onSort={handleSort}
            onColumnFilter={handleColumnFilter}
            onBoolFilter={handleBoolFilter}
            onSelectFilterToggle={handleSelectFilterToggle}
            onSelectFilterClear={handleSelectFilterClear}
            onDragStart={handleDragStart}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onDragEnd={handleDragEnd}
          />
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={parentColSpan} className="dt__empty-cell">
                  No {config.entityLabel} found
                </td>
              </tr>
            ) : (
              items.map((row) => {
                const rowId = row[config.idCol];
                const isExpanded = expanded[rowId];
                const mainPreviewKey = level === "series"
                  ? `series:${row.seriesinstanceuid}`
                  : level === "study" ? `study:${row.studyinstanceuid}` : null;
                const isActivePreview = mainPreviewKey && activeRowKey === mainPreviewKey;
                return (
                  <Fragment key={rowId}>
                    <tr
                      className={`dt__row dt__row--level-${level}${config.expandable ? " dt__row--expandable" : ""}${
                        level === "study" || level === "series" ? " dt__row--previewable" : ""
                      }${isActivePreview ? " dt__row--active" : ""}`}
                      onClick={() => handleMainRowClick(rowId, row)}
                    >
                      {config.expandable && (
                        <td className={`dt__expand-cell${frozenFirstCol ? " dt__expand-cell--frozen" : ""}`}>
                          <span className={`dt__expand-arrow ${isExpanded ? "rotate-90" : ""}`}>{"\u25B6"}</span>
                        </td>
                      )}
                      {mainTableCols.map((c, idx) => {
                        const isNarrow = c.builtin && (c.sourceKey === "patient_id" || c.sourceKey === "stroke_date");
                        return (
                          <td key={c.key}
                            className={`dt__td${frozenFirstCol && idx === 0
                              ? config.expandable ? " dt__td--frozen-first-offset" : " dt__td--frozen-first" : ""}${
                              isNarrow ? " dt__td--narrow" : ""}${!c.builtin ? " dt__td--label" : ""}`}
                            onClick={!c.builtin && (config.expandable || level === "series") ? (e) => e.stopPropagation() : undefined}
                          >
                            {renderCellValue(row, c)}
                          </td>
                        );
                      })}
                      {showActions && (
                        <td className="dt__td--actions" onClick={(e) => e.stopPropagation()}>
                          {renderActions(row, level)}
                        </td>
                      )}
                    </tr>
                    {config.expandable && isExpanded && (
                      <ChildRows
                        parentRowId={rowId}
                        childRows={childRowsData}
                        childConfig={childConfig}
                        childCols={childCols}
                        childIsExpandable={childIsExpandable}
                        parentColSpan={parentColSpan}
                        grandExpanded={grandExpanded}
                        grandChildRows={grandChildRows}
                        grandChildCols={grandChildCols}
                        grandChildConfig={grandChildConfig}
                        gcColSpan={gcColSpan}
                        activeRowKey={activeRowKey}
                        isAdmin={isAdmin}
                        downloadingSeries={downloadingSeries}
                        onChildRowClick={handleChildRowClick}
                        onGrandChildRowClick={handleGrandChildRowClick}
                        onResolveOhifLink={handleOhifLink}
                        onDicomDownload={handleDicomDownload}
                        onMutated={handleMutated}
                      />
                    )}
                  </Fragment>
                );
              })
            )}
            {items.length > 0 && (
              <tr ref={sentinelRef} className="dt__sentinel" aria-hidden="true">
                <td colSpan={parentColSpan}>
                  {loading ? "Loading more…" : !hasMore ? "— end —" : ""}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="dt__footer">
        <div className="dt__footer-left">
          {currentUser && (
            <>
              <button
                onClick={handleRefreshLabelledTables}
                disabled={refreshingLabelledTables}
                className="pill-btn"
              >
                {refreshingLabelledTables ? "Refreshing…" : "Refresh Labelled Tables"}
              </button>
              <button
                onClick={handleRefreshSnapshots}
                disabled={refreshingSnapshots}
                className="pill-btn"
              >
                {refreshingSnapshots ? "Refreshing…" : "Refresh Snapshots"}
              </button>
            </>
          )}
        </div>
        <div className="dt__footer-slot">
          {previewOpen && (
            <div className="dt__pane-tabs">
              {previewUrl && (
                <a
                  href={previewUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="dt__pane-tab"
                >
                  Open in New Tab <span aria-hidden="true">↗</span>
                </a>
              )}
              <button
                type="button"
                onClick={onPreviewClose}
                className="dt__pane-tab"
              >
                Collapse
              </button>
            </div>
          )}
        </div>
        <div className="dt__footer-center">
          <span className="dt__footer-count">
            {countLabel}
            {loading && items.length > 0 ? " · loading…" : ""}
          </span>
        </div>
        <div className="dt__footer-slot" aria-hidden="true" />
        <div className="dt__footer-right">
          <div className="dt__font-controls" title={`Table font size: ${fontScalePct}%`}>
            <button
              type="button"
              onClick={() => adjustFontScale(-0.05)}
              disabled={fontScale <= 0.85}
              className="pill-btn"
              aria-label="Decrease table font size"
            >
              A−
            </button>
            <button
              type="button"
              onClick={() => adjustFontScale(0.05)}
              disabled={fontScale >= 1.25}
              className="pill-btn"
              aria-label="Increase table font size"
            >
              A+
            </button>
          </div>
        </div>
      </div>

      {showDefModal && (
        <LabelDefModal
          defaultLevel={level}
          onClose={() => setShowDefModal(false)}
          onSaved={() => { setShowDefModal(false); fetchLabelDefs(); }}
        />
      )}

      {editingLabel && (
        <LabelDefModal
          existingLabel={editingLabel}
          onClose={() => setEditingLabel(null)}
          onSaved={() => { setEditingLabel(null); fetchLabelDefs(); }}
        />
      )}
    </div>
  );
}

export default function DataTable(props) {
  const { currentUser } = useAuth();
  const [serverPrefs, setServerPrefs] = useState(undefined);

  useEffect(() => {
    let cancelled = false;
    setServerPrefs(undefined);
    apiGet(`/api/preferences/${props.level}`)
      .then((data) => { if (!cancelled) setServerPrefs(data.prefs || {}); })
      .catch(() => { if (!cancelled) setServerPrefs({}); });
    return () => { cancelled = true; };
  }, [props.level, currentUser]);

  if (serverPrefs === undefined) {
    return <div className="dt__panel" style={{ padding: "2rem", opacity: 0.5 }}>Loading preferences\u2026</div>;
  }

  return <DataTableInner {...props} serverPrefs={serverPrefs} />;
}

DataTable.propTypes = {
  level: PropTypes.oneOf(["patient", "study", "series"]).isRequired,
  filters: PropTypes.object.isRequired,
  onResetSidebarFilters: PropTypes.func,
  onPreviewSelect: PropTypes.func,
  activeRowKey: PropTypes.string,
  toolbarPortalTarget: PropTypes.instanceOf(Element),
  previewOpen: PropTypes.bool,
  previewUrl: PropTypes.string,
  onPreviewClose: PropTypes.func,
};
