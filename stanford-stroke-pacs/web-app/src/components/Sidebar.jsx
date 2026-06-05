import { useEffect, useMemo, useState } from "react";
import PropTypes from "prop-types";
import { apiGet } from "../api/client";
import { compareLabelDefsDefault, LEVEL_ORDER, LEVEL_LABELS } from "../utils/table";
import "./Sidebar.css";

const MODALITIES = ["CT", "MR", "CR", "US", "DX", "PT", "NM", "XA", "MG", "RF"];
const UNASSIGNED = "__unassigned__";

// Same default ordering as the data table's columns: instrument groups
// alphabetical (unassigned last), and within each instrument by label
// creation time (oldest first) via the shared compareLabelDefsDefault.
function groupLabelsByInstrument(labels) {
  const groups = new Map();
  for (const l of labels) {
    const key = l.instrument || UNASSIGNED;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(l);
  }
  return Array.from(groups.entries())
    .map(([key, ls]) => ({
      key,
      name: key === UNASSIGNED ? "Unassigned" : key,
      labels: [...ls].sort(compareLabelDefsDefault),
    }))
    .sort((a, b) => {
      if (a.key === UNASSIGNED) return 1;
      if (b.key === UNASSIGNED) return -1;
      return a.name.localeCompare(b.name);
    });
}

export default function Sidebar({ level, filters, onFilterChange, open, onToggle }) {
  const [labelSummary, setLabelSummary] = useState([]);
  const [studyImportLabels, setStudyImportLabels] = useState([]);
  const [datasets, setDatasets] = useState([]);

  const fetchLabels = async () => {
    try {
      const data = await apiGet("/api/labels/summary");
      setLabelSummary(data);
    } catch {
      setLabelSummary([]);
    }
  };

  useEffect(() => {
    fetchLabels();
  }, []);

  useEffect(() => {
    if (level !== "patient") {
      setStudyImportLabels([]);
      setDatasets([]);
      return;
    }
    let cancelled = false;
    apiGet("/api/study-import-labels")
      .then((data) => {
        if (!cancelled && Array.isArray(data)) setStudyImportLabels(data);
      })
      .catch(() => {
        if (!cancelled) setStudyImportLabels([]);
      });
    apiGet("/api/datasets")
      .then((data) => {
        if (!cancelled && Array.isArray(data)) setDatasets(data);
      })
      .catch(() => {
        if (!cancelled) setDatasets([]);
      });
    return () => { cancelled = true; };
  }, [level]);

  useEffect(() => {
    window.__refreshLabelSidebar = fetchLabels;
    return () => { delete window.__refreshLabelSidebar; };
  }, []);

  const handleLabelClick = (label, labelLevel) => {
    if (filters.label === label && filters.labelLevel === labelLevel) {
      onFilterChange({ label: null, labelLevel: null });
    } else {
      onFilterChange({ label, labelLevel });
    }
  };

  const groupedByLevel = useMemo(() => {
    const out = {};
    for (const l of labelSummary) {
      const lvl = l.level || "series";
      if (!out[lvl]) out[lvl] = [];
      out[lvl].push(l);
    }
    for (const lvl of Object.keys(out)) {
      out[lvl] = groupLabelsByInstrument(out[lvl]);
    }
    return out;
  }, [labelSummary]);

  const [collapsedLevels, setCollapsedLevels] = useState(() => new Set());
  const [collapsedInstruments, setCollapsedInstruments] = useState(() => new Set());

  const toggleLevel = (lvl) => {
    setCollapsedLevels((prev) => {
      const next = new Set(prev);
      if (next.has(lvl)) next.delete(lvl); else next.add(lvl);
      return next;
    });
  };

  const toggleInstrument = (lvl, key) => {
    const k = `${lvl}:${key}`;
    setCollapsedInstruments((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  };

  return (
    <>
      <aside className={`sidebar${open ? "" : " sidebar--closed"}`} aria-hidden={!open}>
        <div className="sidebar__inner">
          <h1 className="sidebar__group-title">Quick Filters</h1>

          {/* Dataset (patient view only) */}
          {level === "patient" && (
            <div className="sidebar__section">
              <h2 className="sidebar__section-title">Dataset</h2>
              <div className="sidebar__filter-group">
                <select
                  id="sidebar-dataset"
                  value={filters.dataset || ""}
                  onChange={(e) =>
                    onFilterChange({ dataset: e.target.value || null })
                  }
                  className="sidebar__modality-select"
                >
                  <option value="">All datasets</option>
                  {datasets.map((ds) => (
                    <option key={ds} value={ds}>
                      {ds}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}

          {/* Study Import Label (patient view only) */}
          {level === "patient" && (
            <div className="sidebar__section">
              <h2 className="sidebar__section-title">Study Import Label</h2>
              <div className="sidebar__filter-group">
                <select
                  id="sidebar-study-import-label"
                  value={filters.studyImportLabel || ""}
                  onChange={(e) =>
                    onFilterChange({ studyImportLabel: e.target.value || null })
                  }
                  className="sidebar__modality-select"
                >
                  <option value="">All import labels</option>
                  {studyImportLabels.map((lbl) => (
                    <option key={lbl} value={lbl}>
                      {lbl}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}

          {/* Modality (study/series view) */}
          {(level === "study" || level === "series") && (
            <div className="sidebar__section">
              <h2 className="sidebar__section-title">Modality</h2>
              <div className="sidebar__filter-group">
                <select
                  value={filters.modality || ""}
                  onChange={(e) =>
                    onFilterChange({ modality: e.target.value || null })
                  }
                  className="sidebar__modality-select"
                >
                  <option value="">All modalities</option>
                  {MODALITIES.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}

          {/* Annotation Labels */}
          <div className="sidebar__section">
            <h2 className="sidebar__section-title">Annotation Labels</h2>
            {filters.label && (
              <button
                onClick={() => onFilterChange({ label: null, labelLevel: null })}
                className="sidebar__clear-filter"
              >
                Clear filter
              </button>
            )}
            {Object.keys(groupedByLevel).length === 0 ? (
              <p className="sidebar__empty-msg">No annotations yet</p>
            ) : (
              LEVEL_ORDER.filter((lvl) => groupedByLevel[lvl]).map((lvl) => {
                const isLevelCollapsed = collapsedLevels.has(lvl);
                return (
                  <div key={lvl} className="sidebar__level-group">
                    <div
                      className={`sidebar__level-heading sidebar__level-heading--${lvl}`}
                      onClick={() => toggleLevel(lvl)}
                      role="button"
                      tabIndex={0}
                      aria-expanded={!isLevelCollapsed}
                    >
                      {LEVEL_LABELS[lvl]}
                    </div>
                    {!isLevelCollapsed && groupedByLevel[lvl].map(({ key, name, labels }) => {
                      const instrumentKey = `${lvl}:${key}`;
                      const isInstrumentCollapsed = collapsedInstruments.has(instrumentKey);
                      return (
                        <div key={instrumentKey} className="sidebar__instrument-group">
                          <div
                            className="sidebar__instrument-heading"
                            onClick={() => toggleInstrument(lvl, key)}
                            role="button"
                            tabIndex={0}
                            aria-expanded={!isInstrumentCollapsed}
                          >
                            {name}
                          </div>
                          {!isInstrumentCollapsed && (
                            <ul className="sidebar__label-list">
                              {labels.map((l) => {
                                const isActive = filters.label === l.label && filters.labelLevel === lvl;
                                return (
                                  <li
                                    key={`${lvl}:${l.label}`}
                                    onClick={() => handleLabelClick(l.label, lvl)}
                                    data-full-label={l.label}
                                    aria-label={l.label}
                                    className={`sidebar__label-item ${
                                      isActive
                                        ? "sidebar__label-item--active"
                                        : "sidebar__label-item--inactive"
                                    }`}
                                  >
                                    <span className="sidebar__label-text">{l.label}</span>
                                    <span className="sidebar__label-count">{l.count}</span>
                                  </li>
                                );
                              })}
                            </ul>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </aside>

      <button
        type="button"
        onClick={onToggle}
        aria-label={open ? "Hide sidebar" : "Show sidebar"}
        title={open ? "Hide sidebar" : "Show sidebar"}
        className={`sidebar__toggle${open ? "" : " sidebar__toggle--closed"}`}
      >
        <span aria-hidden="true">{open ? "‹" : "›"}</span>
      </button>
    </>
  );
}

Sidebar.propTypes = {
  level: PropTypes.oneOf(["patient", "study", "series"]).isRequired,
  filters: PropTypes.object.isRequired,
  onFilterChange: PropTypes.func.isRequired,
  open: PropTypes.bool.isRequired,
  onToggle: PropTypes.func.isRequired,
};
