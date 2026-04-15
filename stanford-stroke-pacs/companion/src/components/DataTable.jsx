import { useState, useEffect, useCallback, useRef, useMemo, Fragment } from "react";
import { createPortal } from "react-dom";
import { apiGet, apiFetch } from "../api/client";
import { resolveOhifViewerUrl } from "../api/warmOhif";
import { useColumnPrefs } from "./ColumnSelector";
import ColumnSelector from "./ColumnSelector";
import Pagination from "./Pagination";
import InlineEdit from "./InlineEdit";
import LabelDefModal from "./LabelDefModal";
import { useAuth } from "../context/AuthContext";
import "./DataTable.css";

const DownloadIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ display: "inline-block", verticalAlign: "middle" }}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
);

const PER_PAGE = 50;
const LEVEL_RANK = { patient: 0, study: 1, series: 2 };
const LEVEL_ORDER = ["patient", "study", "series"];

const LEVEL_CONFIG = {
  patient: {
    endpoint: "/api/patients",
    itemsKey: "items",
    idCol: "patient_id",
    entityLabel: "patients",
    builtinCols: [
      { key: "patient_id", label: "Patient ID", filterable: true },
      { key: "stroke_date", label: "Stroke Date", filterable: true },
      { key: "study_import_labels", label: "Study import labels", filterable: true, sortable: false },
    ],
    sortDefault: "patient_id",
    // Note: the displayed column is aggregated `study_import_labels`, but the API filter is the exact-match `study_import_label`.
    filterParamMap: { patient_id: "patient_id", stroke_date: "stroke_date", study_import_labels: "study_import_label" },
    expandable: true,
    expandEndpoint: (row) => `/api/patients/${encodeURIComponent(row.patient_id)}/studies`,
    childLevel: "study",
  },
  study: {
    endpoint: "/api/studies",
    itemsKey: "items",
    idCol: "studyinstanceuid",
    entityLabel: "studies",
    builtinCols: [
      { key: "patient_id", label: "Patient ID", filterable: true },
      { key: "import_id", label: "Import ID", filterable: true, defaultVisible: false },
      { key: "import_label", label: "Import Label", filterable: true },
      { key: "acquisitiondatetime", label: "Acquisition Date", filterable: true },
      { key: "modality", label: "Modality", filterable: true },
      { key: "studydescription", label: "Study Description", filterable: true },
    ],
    sortDefault: "patient_id",
    filterParamMap: {
      patient_id: "patient_id",
      import_id: "import_id",
      import_label: "import_label",
      acquisitiondatetime: "acquisitiondatetime",
      modality: "modality",
      studydescription: "studydescription",
    },
    expandable: true,
    expandEndpoint: (row) => `/api/studies/${encodeURIComponent(row.studyinstanceuid)}/series`,
    childLevel: "series",
  },
  series: {
    endpoint: "/api/series",
    itemsKey: "series",
    idCol: "seriesinstanceuid",
    entityLabel: "series",
    builtinCols: [
      { key: "patient_id", label: "Patient ID", filterable: true },
      { key: "import_id", label: "Import ID", filterable: true, defaultVisible: false },
      { key: "import_label", label: "Import Label", filterable: true},
      { key: "acquisitiondatetime", label: "Acquisition Date", filterable: true },
      { key: "modality", label: "Modality", filterable: true },
      { key: "seriesdescription", label: "Series Description", filterable: true },
      { key: "number_of_slices", label: "Slices", filterable: false },
    ],
    sortDefault: "patient_id",
    filterParamMap: {
      patient_id: "patient_id",
      import_id: "import_id",
      import_label: "import_label",
      acquisitiondatetime: "acquisitiondatetime",
      modality: "modality",
      seriesdescription: "description",
    },
    expandable: false,
  },
};

function buildBuiltinColumnCatalog(activeLevel) {
  return LEVEL_ORDER.flatMap((builtinLevel) =>
    LEVEL_CONFIG[builtinLevel].builtinCols.map((col) => ({
      ...col,
      key: `builtin:${builtinLevel}:${col.key}`,
      sourceKey: col.key,
      level: builtinLevel,
      defaultVisible:
        LEVEL_RANK[builtinLevel] >= LEVEL_RANK[activeLevel] && col.defaultVisible !== false,
      legacyKeys:
        builtinLevel === activeLevel
          ? [col.key, ...(col.key === "import_id" ? ["integration_id"] : [])]
          : [],
    })),
  );
}


function formatDatetime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-CA") + " " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

/** Patient expandable studies; optional filter matches `image_study.import_label` exactly. */
function buildPatientStudiesUrl(row, studyImportLabel) {
  const base = `/api/patients/${encodeURIComponent(row.patient_id)}/studies`;
  const v = typeof studyImportLabel === "string" ? studyImportLabel.trim() : "";
  if (!v) return base;
  return `${base}?study_import_label=${encodeURIComponent(v)}`;
}

function normalizeSelectFilterValues(value) {
  if (Array.isArray(value)) {
    return value
      .map((item) => (typeof item === "string" ? item.trim() : ""))
      .filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) {
    return [value.trim()];
  }
  return [];
}

function hasFilterValue(value) {
  if (Array.isArray(value)) {
    return normalizeSelectFilterValues(value).length > 0;
  }
  return value != null && value !== "";
}

function getTextFilterValue(value) {
  return typeof value === "string" ? value : "";
}

function SelectFilterControl({ options = [], value, onToggle, onClear }) {
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
  const uniqueOptions = [...new Set(options.map((option) => String(option).trim()).filter(Boolean))].sort();
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
        <span className="dt__select-filter-caret">{open ? "▲" : "▼"}</span>
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

function DataTableInner({
  level,
  filters,
  page,
  onPageChange,
  onPreviewSelect,
  activeRowKey,
  toolbarPortalTarget,
  serverPrefs,
}) {
  const { currentUser } = useAuth();
  const config = LEVEL_CONFIG[level];
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [labelDefs, setLabelDefs] = useState([]);
  const [showDefModal, setShowDefModal] = useState(false);
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

  const [expanded, setExpanded] = useState({});
  const [childRows, setChildRows] = useState({});
  const [grandExpanded, setGrandExpanded] = useState({});
  const [grandChildRows, setGrandChildRows] = useState({});

  const builtinCols = useMemo(() => buildBuiltinColumnCatalog(level), [level]);
  const levelLabelDefs = labelDefs;
  const forcedVisibleKeys = filters.label ? [`label:${filters.label}`] : [];

  const { allCols, visibleCols, visibleKeys, columnOrder, effectiveVisibleKeys, toggle, reorder, resetColumns } =
    useColumnPrefs(levelLabelDefs, builtinCols, level, forcedVisibleKeys, serverPrefs);

  const totalPages = Math.ceil(total / PER_PAGE);

  const dragColKey = useRef(null);
  const [dragOverKey, setDragOverKey] = useState(null);
  const [dropSide, setDropSide] = useState(null);

  const latestPrefs = useRef({});
  const dirty = useRef(false);
  latestPrefs.current = {
    visibleKeys,
    columnOrder,
    sortBy,
    sortDir,
    columnFilters: Object.fromEntries(
      Object.entries(columnFilters).filter(([, v]) => hasFilterValue(v)),
    ),
    freezeFirstCol: frozenFirstCol,
  };

  const saveTimer = useRef(null);
  const initialRender = useRef(true);

  const flushSave = useCallback(() => {
    clearTimeout(saveTimer.current);
    if (!currentUser || !dirty.current) return;
    dirty.current = false;
    fetch(`/api/preferences/${level}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      keepalive: true,
      body: JSON.stringify({ prefs: latestPrefs.current }),
    }).catch(() => {});
  }, [currentUser, level]);

  useEffect(() => {
    if (initialRender.current) {
      initialRender.current = false;
      return;
    }
    if (!currentUser) return;
    dirty.current = true;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      dirty.current = false;
      apiFetch(`/api/preferences/${level}`, {
        method: "PUT",
        body: JSON.stringify({ prefs: latestPrefs.current }),
      }).catch(() => {});
    }, 800);
    return () => clearTimeout(saveTimer.current);
  }, [currentUser, level, visibleKeys, columnOrder, sortBy, sortDir, columnFilters, frozenFirstCol]);

  useEffect(() => {
    const handleUnload = () => flushSave();
    window.addEventListener("beforeunload", handleUnload);
    return () => {
      window.removeEventListener("beforeunload", handleUnload);
      flushSave();
    };
  }, [flushSave]);

  const fetchLabelDefs = useCallback(async () => {
    try {
      setLabelDefs(await apiGet("/api/label-definitions"));
    } catch {
      setLabelDefs([]);
    }
  }, []);

  const fetchItems = useCallback(async () => {
    const params = new URLSearchParams({
      page: String(page),
      per_page: String(PER_PAGE),
      sort_by: sortBy,
      sort_dir: sortDir,
    });

    if (filters.label) {
      params.set("label", filters.label);
      if (filters.labelLevel) params.set("label_level", filters.labelLevel);
    }
    if (filters.patientId) params.set("patient_id", filters.patientId);
    if (filters.modality) params.set("modality", filters.modality);
    if (filters.description) params.set("description", filters.description);
    if (level === "patient" && filters.studyImportLabel?.trim()) {
      params.set("study_import_label", filters.studyImportLabel.trim());
    }

    const labelFilters = [];
    for (const [key, val] of Object.entries(columnFilters)) {
      if (!hasFilterValue(val)) continue;
      if (key.startsWith("label:")) {
        const col = allCols.find((c) => c.key === key);
        const datatype = col?.datatype || "text";
        if (datatype === "select") {
          const values = normalizeSelectFilterValues(val);
          if (values.length === 0) continue;
          labelFilters.push({
            label: key.replace("label:", ""),
            level: col?.level || level,
            values,
            datatype,
          });
        } else {
          labelFilters.push({
            label: key.replace("label:", ""),
            level: col?.level || level,
            value: getTextFilterValue(val),
            datatype,
          });
        }
      } else {
        const param = config.filterParamMap[key];
        if (param && typeof val === "string") params.set(param, val);
      }
    }
    if (labelFilters.length > 0) {
      params.set("label_filters", JSON.stringify(labelFilters));
    }

    try {
      const data = await apiGet(`${config.endpoint}?${params}`);
      setItems(data[config.itemsKey]);
      setTotal(data.total);
    } catch {
      setItems([]);
      setTotal(0);
    }
  }, [page, filters, sortBy, sortDir, columnFilters, config, allCols, level]);

  useEffect(() => {
    if (level !== "patient") return;
    setChildRows({});
    setExpanded({});
  }, [level, filters.studyImportLabel]);

  useEffect(() => { fetchLabelDefs(); }, [fetchLabelDefs]);
  useEffect(() => { fetchItems(); }, [fetchItems]);

  const handleMutated = () => {
    fetchItems();
    for (const [rowId, isExp] of Object.entries(expanded)) {
      if (!isExp) continue;
      const row = items.find((r) => r[config.idCol] === rowId);
      if (row && config.expandEndpoint) {
        const url =
          level === "patient"
            ? buildPatientStudiesUrl(row, filters.studyImportLabel)
            : config.expandEndpoint(row);
        apiGet(url)
          .then((data) => setChildRows((prev) => ({ ...prev, [rowId]: data })))
          .catch(() => {});
      }
    }
    if (childConfig?.expandEndpoint) {
      for (const [gcKey, isExp] of Object.entries(grandExpanded)) {
        if (!isExp) continue;
        const [parentId, childId] = gcKey.split("::");
        const parentChildren = childRows[parentId];
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
      onPageChange(1);
    }, 400);
  };

  const handleBoolFilter = (key) => {
    clearTimeout(filterTimeout.current);
    setColumnFilters((prev) => {
      const cur = prev[key];
      const next = cur === "true" ? "false" : cur === "false" ? null : "true";
      return { ...prev, [key]: next };
    });
    onPageChange(1);
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
    onPageChange(1);
  };

  const handleSelectFilterClear = (key) => {
    clearTimeout(filterTimeout.current);
    setColumnFilters((prev) => ({ ...prev, [key]: null }));
    onPageChange(1);
  };

  const handleDragStart = (key, e) => {
    dragColKey.current = key;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", key);
  };

  const handleDragOver = (key, e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (dragColKey.current === null || dragColKey.current === key) {
      setDragOverKey(null);
      setDropSide(null);
      return;
    }
    const rect = e.currentTarget.getBoundingClientRect();
    const side = e.clientX < rect.left + rect.width / 2 ? "before" : "after";
    setDragOverKey(key);
    setDropSide(side);
  };

  const handleDragLeave = () => {
    setDragOverKey(null);
    setDropSide(null);
  };

  const handleDrop = (key, e) => {
    e.preventDefault();
    const fromKey = dragColKey.current;
    if (fromKey && fromKey !== key) {
      const rect = e.currentTarget.getBoundingClientRect();
      const side = e.clientX < rect.left + rect.width / 2 ? "before" : "after";
      reorder(fromKey, key, side);
    }
    dragColKey.current = null;
    setDragOverKey(null);
    setDropSide(null);
  };

  const handleDragEnd = () => {
    dragColKey.current = null;
    setDragOverKey(null);
    setDropSide(null);
  };

  const resolveOhifLink = async (studyinstanceuid, seriesinstanceuid = null) => {
    try {
      const url = await resolveOhifViewerUrl(studyinstanceuid, seriesinstanceuid);
      if (url) window.open(url, "_blank");
    } catch (e) {
      alert(e?.message || "Could not resolve OHIF link");
    }
  };

  const handleRefreshSnapshots = async () => {
    setRefreshingSnapshots(true);
    try {
      const res = await apiFetch("/api/snapshots/refresh", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        alert(`Snapshots refreshed.\n${Object.entries(data.counts).map(([k, v]) => `${k}: ${v} rows`).join("\n")}`);
      } else {
        alert("Failed to refresh snapshots");
      }
    } catch {
      alert("Failed to refresh snapshots");
    } finally {
      setRefreshingSnapshots(false);
    }
  };

  const handleRefreshLabelledTables = async () => {
    setRefreshingLabelledTables(true);
    try {
      const res = await apiFetch("/api/labelled-tables/refresh", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        alert(`Labelled tables refreshed.\n${Object.entries(data.counts).map(([k, v]) => `${k}: ${v} rows`).join("\n")}`);
      } else {
        alert("Failed to refresh labelled tables");
      }
    } catch {
      alert("Failed to refresh labelled tables");
    } finally {
      setRefreshingLabelledTables(false);
    }
  };

  const toggleExpand = async (rowId, row) => {
    if (expanded[rowId]) {
      setExpanded((prev) => ({ ...prev, [rowId]: false }));
      return;
    }
    setExpanded((prev) => ({ ...prev, [rowId]: true }));
    if (!childRows[rowId]) {
      try {
        const url =
          level === "patient"
            ? buildPatientStudiesUrl(row, filters.studyImportLabel)
            : config.expandEndpoint(row);
        const data = await apiGet(url);
        setChildRows((prev) => ({ ...prev, [rowId]: data }));
      } catch {
        setChildRows((prev) => ({ ...prev, [rowId]: [] }));
      }
    }
  };

  const allAnnotations = (row) => [
    ...(row.annotations || []),
    ...(row.inherited_annotations || []),
  ];

  const selectPreview = (row, sourceLevel) => {
    if (!onPreviewSelect || !row?.studyinstanceuid) return;
    onPreviewSelect({
      rowKey:
        sourceLevel === "series"
          ? `series:${row.seriesinstanceuid}`
          : `study:${row.studyinstanceuid}`,
      studyinstanceuid: row.studyinstanceuid,
      seriesinstanceuid: sourceLevel === "series" ? row.seriesinstanceuid || null : null,
      sourceLevel,
      patientId: row.patient_id || null,
      description:
        sourceLevel === "series"
          ? row.seriesdescription || null
          : row.studydescription || null,
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

  const [downloadingSeries, setDownloadingSeries] = useState(null);

  const handleDicomDownload = async (seriesinstanceuid) => {
    setDownloadingSeries(seriesinstanceuid);
    try {
      const res = await fetch(`/api/series/${encodeURIComponent(seriesinstanceuid)}/dicom-zip`, {
        credentials: "same-origin",
      });
      if (!res.ok) {
        const text = await res.text();
        alert(`Download failed: ${text || res.statusText}`);
        return;
      }
      const cd = res.headers.get("Content-Disposition") || "";
      const match = cd.match(/filename="?([^"]+)"?/);
      const fname = match ? match[1] : `${seriesinstanceuid}.zip`;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(`Download failed: ${err.message}`);
    } finally {
      setDownloadingSeries(null);
    }
  };

  const renderActions = (row, rowLevel) => {
    const uid = row.studyinstanceuid;
    if (uid && (rowLevel === "study" || rowLevel === "series")) {
      return (
        <>
          <button
            onClick={() => resolveOhifLink(uid, rowLevel === "series" ? row.seriesinstanceuid : null)}
            className="link-btn"
          >
            OHIF
          </button>
          {rowLevel === "series" && row.seriesinstanceuid && (
            <button
              onClick={() => handleDicomDownload(row.seriesinstanceuid)}
              className="link-btn"
              title="Download DICOM as zip"
              disabled={downloadingSeries === row.seriesinstanceuid}
            >
              {downloadingSeries === row.seriesinstanceuid ? "…" : <DownloadIcon />}
            </button>
          )}
        </>
      );
    }
    return null;
  };

  const childConfig = config.expandable ? LEVEL_CONFIG[config.childLevel] : null;
  const grandChildConfig = childConfig?.expandable ? LEVEL_CONFIG[childConfig.childLevel] : null;
  const selectedBuiltinCols = allCols.filter(
    (c) => c.builtin && effectiveVisibleKeys.includes(c.key),
  );
  const selectedLabelCols = visibleCols.filter((c) => !c.builtin);
  const getBuiltinColsForLevel = (targetLevel) =>
    selectedBuiltinCols.filter((c) => c.level === targetLevel);
  const getLabelColsForLevel = (targetLevel) =>
    selectedLabelCols.filter((c) => c.level === targetLevel);

  const childBuiltinCols = childConfig
    ? getBuiltinColsForLevel(config.childLevel)
    : [];
  const childLabelCols = childConfig
    ? getLabelColsForLevel(config.childLevel)
    : [];
  const childCols = [...childBuiltinCols, ...childLabelCols];

  const grandChildBuiltinCols = grandChildConfig
    ? getBuiltinColsForLevel(childConfig.childLevel)
    : [];
  const grandChildLabelCols = grandChildConfig
    ? getLabelColsForLevel(childConfig.childLevel)
    : [];
  const grandChildCols = [...grandChildBuiltinCols, ...grandChildLabelCols];

  const mainTableCols = visibleCols.filter(
    (c) => (c.builtin ? c.level === level : LEVEL_RANK[c.level] <= LEVEL_RANK[level])
  );
  const showActions = level !== "patient";
  const parentColSpan = mainTableCols.length + (config.expandable ? 1 : 0) + (showActions ? 1 : 0);
  const childIsExpandable = !!grandChildConfig;

  const toggleGrandExpand = async (key, childRow) => {
    if (grandExpanded[key]) {
      setGrandExpanded((prev) => ({ ...prev, [key]: false }));
      return;
    }
    setGrandExpanded((prev) => ({ ...prev, [key]: true }));
    if (!grandChildRows[key]) {
      try {
        const data = await apiGet(childConfig.expandEndpoint(childRow));
        setGrandChildRows((prev) => ({ ...prev, [key]: data }));
      } catch {
        setGrandChildRows((prev) => ({ ...prev, [key]: [] }));
      }
    }
  };

  const gcColSpan = childCols.length + (childIsExpandable ? 2 : 1);

  const handleMainRowClick = (rowId, row) => {
    if (level === "study") {
      if (expanded[rowId]) {
        toggleExpand(rowId, row);
        return;
      }
      selectPreview(row, "study");
    } else if (level === "series") {
      selectPreview(row, "series");
    }
    if (config.expandable) {
      toggleExpand(rowId, row);
    }
  };

  const handleChildRowClick = (gcKey, child) => {
    if (config.childLevel === "study") {
      if (grandExpanded[gcKey]) {
        toggleGrandExpand(gcKey, child);
        return;
      }
      selectPreview(child, "study");
    } else if (config.childLevel === "series") {
      selectPreview(child, "series");
    }
    if (childIsExpandable) {
      toggleGrandExpand(gcKey, child);
    }
  };

  const handleGrandChildRowClick = (row) => {
    selectPreview(row, "series");
  };

  const renderGrandChildRows = (gcKey) => {
    const grandChildren = grandChildRows[gcKey];
    if (!grandChildren || grandChildren.length === 0) {
      return (
        <tr>
          <td colSpan={gcColSpan} className="dt__gc-empty">
            No series found
          </td>
        </tr>
      );
    }
    return (
      <tr>
        <td colSpan={gcColSpan} className="dt__gc-wrapper">
          <div className="dt__gc-scroll">
            <table className="dt__gc-table">
              <thead className="dt__gc-thead">
                <tr className="dt__gc-head-row">
                  {grandChildCols.map((c) => (
                    <th key={c.key} className="dt__gc-th">
                      {c.label}
                    </th>
                  ))}
                  <th className="dt__gc-th">Actions</th>
                </tr>
              </thead>
              <tbody>
                {grandChildren.map((gc) => {
                  const gcId = gc[grandChildConfig.idCol];
                  const gcAnnotations = [...(gc.annotations || []), ...(gc.inherited_annotations || [])];
                  const isActivePreview = activeRowKey === `series:${gc.seriesinstanceuid}`;
                  return (
                    <tr
                      key={gcId}
                      className={`dt__gc-row dt__gc-row--previewable ${
                        isActivePreview ? "dt__gc-row--active" : ""
                      }`}
                      onClick={() => handleGrandChildRowClick(gc)}
                    >
                      {grandChildCols.map((c) => {
                        if (c.builtin) {
                          const raw = gc[c.sourceKey] ?? "";
                          const display = c.sourceKey === "acquisitiondatetime" ? formatDatetime(raw) : raw;
                          return (
                            <td key={c.key} className="dt__gc-td">
                              {display}
                            </td>
                          );
                        }
                        const labelName = c.key.replace("label:", "");
                        return (
                          <td key={c.key} className="dt__gc-td" onClick={(e) => e.stopPropagation()}>
                            <InlineEdit
                              level={c.level}
                              entity={gc}
                              labelName={labelName}
                              datatype={c.datatype}
                              defOptions={c.options || []}
                              annotations={gcAnnotations}
                              onMutated={handleMutated}
                            />
                          </td>
                        );
                      })}
                      <td className="dt__gc-td--actions" onClick={(e) => e.stopPropagation()}>
                        {gc.studyinstanceuid && (
                          <button
                            onClick={() => resolveOhifLink(gc.studyinstanceuid, gc.seriesinstanceuid)}
                            className="dt__gc-link-btn"
                          >
                            OHIF
                          </button>
                        )}
                        {gc.seriesinstanceuid && (
                          <button
                            onClick={() => handleDicomDownload(gc.seriesinstanceuid)}
                            className="dt__gc-link-btn"
                            title="Download DICOM as zip"
                            disabled={downloadingSeries === gc.seriesinstanceuid}
                          >
                            {downloadingSeries === gc.seriesinstanceuid ? "…" : <DownloadIcon />}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </td>
      </tr>
    );
  };

  const handleResetDefaults = () => {
    resetColumns();
    setSortBy(config.sortDefault);
    setSortDir("asc");
    setColumnFilters({});
    setFrozenFirstCol(false);
    onPageChange(1);
  };

  const topBarControls = (
    <>
      <ColumnSelector
        allCols={allCols}
        visibleKeys={visibleKeys}
        onToggle={toggle}
      />
      <button onClick={handleResetDefaults} className="btn-outline">
        Reset View
      </button>
      <button
        onClick={() => {
          if (!currentUser) {
            alert("Please log in to create label types");
            return;
          }
          setShowDefModal(true);
        }}
        className="btn-outline"
      >
        + New Label Type
      </button>
    </>
  );

  const renderChildRows = (parentRowId) => {
    const children = childRows[parentRowId];
    if (!children || children.length === 0) {
      return (
        <tr>
          <td colSpan={parentColSpan} className="dt__child-empty">
            No child records found
          </td>
        </tr>
      );
    }
    const childTableContent = (
          <table className="dt__child-table">
            <thead className="dt__child-thead">
              <tr className="dt__child-head-row">
                {childIsExpandable && (
                  <th className="dt__child-th--expand" />
                )}
                {childCols.map((c) => (
                  <th key={c.key} className="dt__child-th">
                    {c.label}
                    {!c.builtin && (
                      <span className="dt__child-datatype-hint">({c.datatype})</span>
                    )}
                  </th>
                ))}
                <th className="dt__child-th">Actions</th>
              </tr>
            </thead>
            <tbody>
              {children.map((child) => {
                const childId = child[childConfig.idCol];
                const gcKey = `${parentRowId}::${childId}`;
                const isGrandExpanded = grandExpanded[gcKey];
                const childAnnotations = [...(child.annotations || []), ...(child.inherited_annotations || [])];
                const childPreviewKey = config.childLevel === "series"
                  ? `series:${child.seriesinstanceuid}`
                  : `study:${child.studyinstanceuid}`;
                const isActivePreview = activeRowKey === childPreviewKey;
                return (
                  <Fragment key={childId}>
                    <tr
                      className={`dt__child-row${
                        childIsExpandable ? " dt__child-row--expandable" : ""
                      }${config.childLevel === "study" || config.childLevel === "series" ? " dt__child-row--previewable" : ""}${
                        isActivePreview ? " dt__child-row--active" : ""
                      }`}
                      onClick={() => handleChildRowClick(gcKey, child)}
                    >
                      {childIsExpandable && (
                        <td className="dt__child-expand-cell">
                          <span className={`dt__child-expand-arrow ${isGrandExpanded ? "rotate-90" : ""}`}>
                            ▶
                          </span>
                        </td>
                      )}
                      {childCols.map((c) => {
                        if (c.builtin) {
                          const raw = child[c.sourceKey] ?? "";
                          const display = c.sourceKey === "acquisitiondatetime" ? formatDatetime(raw) : raw;
                          return (
                            <td key={c.key} className="dt__child-td">
                              {display}
                            </td>
                          );
                        }
                        const labelName = c.key.replace("label:", "");
                        return (
                          <td key={c.key} className="dt__child-td" onClick={(e) => e.stopPropagation()}>
                            <InlineEdit
                              level={c.level}
                              entity={child}
                              labelName={labelName}
                              datatype={c.datatype}
                              defOptions={c.options || []}
                              annotations={childAnnotations}
                              onMutated={handleMutated}
                            />
                          </td>
                        );
                      })}
                      <td className="dt__child-td--actions" onClick={(e) => e.stopPropagation()}>
                        {child.studyinstanceuid && (
                          <button
                            onClick={() => resolveOhifLink(child.studyinstanceuid, config.childLevel === "series" ? child.seriesinstanceuid : null)}
                            className="link-btn"
                          >
                            OHIF
                          </button>
                        )}
                        {config.childLevel === "series" && child.seriesinstanceuid && (
                          <button
                            onClick={() => handleDicomDownload(child.seriesinstanceuid)}
                            className="link-btn"
                            title="Download DICOM as zip"
                            disabled={downloadingSeries === child.seriesinstanceuid}
                          >
                            {downloadingSeries === child.seriesinstanceuid ? "…" : <DownloadIcon />}
                          </button>
                        )}
                      </td>
                    </tr>
                    {childIsExpandable && isGrandExpanded && renderGrandChildRows(gcKey)}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
    );
    const needsScroll = !childIsExpandable;
    return (
      <tr>
        <td colSpan={parentColSpan} className="dt__child-wrapper">
          {needsScroll
            ? <div className="dt__child-scroll">{childTableContent}</div>
            : childTableContent}
        </td>
      </tr>
    );
  };

  return (
    <div className="dt__panel">
      {toolbarPortalTarget ? createPortal(topBarControls, toolbarPortalTarget) : null}

      <div className="dt__summary-bar">
        <div className="dt__summary">
          {total === 0
            ? `0 ${config.entityLabel}`
            : `${total.toLocaleString()} ${config.entityLabel} total — page ${page} of ${totalPages}`}
        </div>
        {currentUser && (
          <div className="dt__summary-actions">
            <button
              onClick={handleRefreshLabelledTables}
              disabled={refreshingLabelledTables}
              className={`dt__summary-action ${refreshingLabelledTables ? "dt__refresh-btn--disabled" : ""}`}
            >
              {refreshingLabelledTables ? "Refreshing..." : "Refresh Labelled Tables"}
            </button>
            <button
              onClick={handleRefreshSnapshots}
              disabled={refreshingSnapshots}
              className={`dt__summary-action ${refreshingSnapshots ? "dt__refresh-btn--disabled" : ""}`}
            >
              {refreshingSnapshots ? "Refreshing..." : "Refresh Snapshots"}
            </button>
          </div>
        )}
      </div>

      <div className="dt__scroll">
        <table className="dt">
          <thead className="dt__head">
            <tr>
              {config.expandable && (
                <th className={`dt__th--expand${frozenFirstCol ? " dt__th--expand-frozen" : ""}`} />
              )}
              {mainTableCols.map((c, idx) => (
                <th
                  key={c.key}
                  draggable
                  onDragStart={(e) => handleDragStart(c.key, e)}
                  onDragOver={(e) => handleDragOver(c.key, e)}
                  onDragLeave={handleDragLeave}
                  onDrop={(e) => handleDrop(c.key, e)}
                  onDragEnd={handleDragEnd}
                  title={!c.builtin ? c.description || "" : undefined}
                  onClick={() => c.builtin && c.sortable !== false && handleSort(c.sourceKey)}
                  className={`dt__th ${c.builtin && c.sortable !== false ? "dt__th--sortable" : ""}${
                    frozenFirstCol && idx === 0
                      ? config.expandable ? " dt__th--frozen-first-offset" : " dt__th--frozen-first"
                      : ""
                  }${dragColKey.current === c.key ? " dt__th--dragging" : ""}${
                    dragOverKey === c.key && dropSide === "before" ? " dt__th--drop-before" : ""
                  }${dragOverKey === c.key && dropSide === "after" ? " dt__th--drop-after" : ""}`}
                >
                  {c.label}
                  {idx === 0 && (
                    <button
                      type="button"
                      className={`dt__pin-btn${frozenFirstCol ? " dt__pin-btn--active" : ""}`}
                      onClick={(e) => { e.stopPropagation(); setFrozenFirstCol((v) => !v); }}
                      title={frozenFirstCol ? "Unfreeze column" : "Freeze column"}
                    >
                      📌
                    </button>
                  )}
                  {c.builtin && c.sortable !== false && sortBy === c.sourceKey && (
                    <span className="dt__sort-arrow">
                      {sortDir === "asc" ? "↑" : "↓"}
                    </span>
                  )}
                  {!c.builtin && (
                    <span className="dt__datatype-hint">
                      {c.datatype}
                      {c.level && c.level !== level && (
                        <span className="dt__level-hint">({c.level})</span>
                      )}
                    </span>
                  )}
                </th>
              ))}
              {showActions && (
                <th className="dt__th">
                  Actions
                </th>
              )}
            </tr>
            <tr>
              {config.expandable && (
                <th className={`dt__filter-th${frozenFirstCol ? " dt__filter-th--expand-frozen" : ""}`} />
              )}
              {mainTableCols.map((c, idx) => (
                <th
                  key={`f-${c.key}`}
                  className={`dt__filter-th${
                    frozenFirstCol && idx === 0
                      ? config.expandable ? " dt__filter-th--frozen-first-offset" : " dt__filter-th--frozen-first"
                      : ""
                  }`}
                >
                  {c.builtin && config.filterParamMap[c.sourceKey] ? (
                    <input
                      type="text"
                      placeholder="Filter…"
                      defaultValue={getTextFilterValue(columnFilters[c.sourceKey])}
                      onChange={(e) => handleColumnFilter(c.sourceKey, e.target.value)}
                      className="dt__filter-input"
                    />
                  ) : !c.builtin && c.datatype === "bool" ? (
                    <button
                      type="button"
                      className={`dt__bool-filter${columnFilters[c.key] ? " dt__bool-filter--active" : ""}`}
                      onClick={() => handleBoolFilter(c.key)}
                      title={
                        columnFilters[c.key] === "true" ? "Showing: true — click for false"
                          : columnFilters[c.key] === "false" ? "Showing: false — click to clear"
                          : "Filter by bool value"
                      }
                    >
                      {columnFilters[c.key] === "true" ? "✓"
                        : columnFilters[c.key] === "false" ? "✗"
                        : "⊘"}
                    </button>
                  ) : !c.builtin && c.datatype === "select" ? (
                    <SelectFilterControl
                      options={c.options || []}
                      value={columnFilters[c.key]}
                      onToggle={(option) => handleSelectFilterToggle(c.key, option)}
                      onClear={() => handleSelectFilterClear(c.key)}
                    />
                  ) : !c.builtin ? (
                    <input
                      type="text"
                      placeholder="Filter…"
                      defaultValue={getTextFilterValue(columnFilters[c.key])}
                      onChange={(e) => handleColumnFilter(c.key, e.target.value)}
                      className="dt__filter-input"
                    />
                  ) : null}
                </th>
              ))}
              {showActions && <th className="dt__filter-th" />}
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td
                  colSpan={parentColSpan}
                  className="dt__empty-cell"
                >
                  No {config.entityLabel} found
                </td>
              </tr>
            ) : (
              items.map((row) => {
                const rowId = row[config.idCol];
                const isExpanded = expanded[rowId];
                const mainPreviewKey = level === "series"
                  ? `series:${row.seriesinstanceuid}`
                  : level === "study"
                    ? `study:${row.studyinstanceuid}`
                    : null;
                const isActivePreview = mainPreviewKey && activeRowKey === mainPreviewKey;
                return (
                  <Fragment key={rowId}>
                    <tr
                      className={`dt__row${
                        config.expandable ? " dt__row--expandable" : ""
                      }${level === "study" || level === "series" ? " dt__row--previewable" : ""}${
                        isActivePreview ? " dt__row--active" : ""
                      }`}
                      onClick={() => handleMainRowClick(rowId, row)}
                    >
                      {config.expandable && (
                        <td className={`dt__expand-cell${frozenFirstCol ? " dt__expand-cell--frozen" : ""}`}>
                          <span className={`dt__expand-arrow ${isExpanded ? "rotate-90" : ""}`}>
                            ▶
                          </span>
                        </td>
                      )}
                      {mainTableCols.map((c, idx) => (
                        <td
                          key={c.key}
                          className={`dt__td${
                            frozenFirstCol && idx === 0
                              ? config.expandable ? " dt__td--frozen-first-offset" : " dt__td--frozen-first"
                              : ""
                          }`}
                          onClick={!c.builtin && (config.expandable || level === "series") ? (e) => e.stopPropagation() : undefined}
                        >
                          {renderCellValue(row, c)}
                        </td>
                      ))}
                      {showActions && (
                        <td className="dt__td--actions" onClick={(e) => e.stopPropagation()}>
                          {renderActions(row, level)}
                        </td>
                      )}
                    </tr>
                    {config.expandable && isExpanded && renderChildRows(rowId)}
                  </Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <Pagination
        page={page}
        totalPages={totalPages}
        onPageChange={onPageChange}
      />

      {showDefModal && (
        <LabelDefModal
          defaultLevel={level}
          onClose={() => setShowDefModal(false)}
          onCreated={() => {
            setShowDefModal(false);
            fetchLabelDefs();
          }}
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
    return <div className="dt__panel" style={{ padding: "2rem", opacity: 0.5 }}>Loading preferences…</div>;
  }

  return <DataTableInner {...props} serverPrefs={serverPrefs} />;
}
