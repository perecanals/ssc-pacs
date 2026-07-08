import { useCallback, useEffect, useMemo, useState } from "react";
import PropTypes from "prop-types";
import { apiGet } from "../api/client";
import { groupByInstrument, LEVEL_ORDER, LEVEL_LABELS } from "../utils/table";
import LabelValueFilter from "./Sidebar/LabelValueFilter";
import "./Sidebar.css";

export default function Sidebar({ level, filters, onFilterChange, open, onToggle }) {
  const [labelSummary, setLabelSummary] = useState([]);
  const [labelDefs, setLabelDefs] = useState([]);
  const [studyImportLabels, setStudyImportLabels] = useState([]);
  const [datasets, setDatasets] = useState([]);
  // Which select-label popup is pinned open (one at a time). Keyed "<level>:<label>".
  const [pinnedKey, setPinnedKey] = useState(null);

  const fetchLabels = useCallback(async () => {
    // Summary feeds the label list + counts; definitions feed each select
    // label's value options (effective: curated ∪ live values). Refreshed
    // together so values created inline appear in the picker without a reload.
    try {
      setLabelSummary(await apiGet("/api/labels/summary"));
    } catch {
      setLabelSummary([]);
    }
    try {
      setLabelDefs(await apiGet("/api/label-definitions"));
    } catch {
      setLabelDefs([]);
    }
  }, []);

  useEffect(() => {
    fetchLabels();
  }, [fetchLabels]);

  // "<level>:<name>" -> { datatype, options } for select-aware rendering.
  const defByKey = useMemo(() => {
    const out = {};
    for (const d of labelDefs) {
      out[`${d.level}:${d.name}`] = {
        datatype: d.datatype,
        options: Array.isArray(d.options) ? d.options : [],
      };
    }
    return out;
  }, [labelDefs]);

  const labelValues = filters.labelValues || {};

  const toggleLabelValue = useCallback(
    (key, value) => {
      const cur = filters.labelValues?.[key] || [];
      const next = cur.includes(value)
        ? cur.filter((v) => v !== value)
        : [...cur, value];
      const map = { ...(filters.labelValues || {}) };
      if (next.length) map[key] = next;
      else delete map[key];
      onFilterChange({ labelValues: map });
    },
    [filters.labelValues, onFilterChange],
  );

  const clearLabelValue = useCallback(
    (key) => {
      const map = { ...(filters.labelValues || {}) };
      delete map[key];
      onFilterChange({ labelValues: map });
    },
    [filters.labelValues, onFilterChange],
  );

  // A single document-level dismissal for whichever popup is pinned.
  useEffect(() => {
    if (!pinnedKey) return undefined;
    const onDown = (e) => {
      // The popup is portaled to <body>, so it is outside .sidebar__lvf — match
      // it explicitly so clicking inside a pinned popup doesn't dismiss it.
      if (!e.target.closest?.(".sidebar__lvf, .sidebar__lvf-popup")) setPinnedKey(null);
    };
    const onKey = (e) => {
      if (e.key === "Escape") setPinnedKey(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [pinnedKey]);

  // Dataset + import-label option lists are level-independent (and scope-filtered
  // server-side), so fetch once. Both feed the sidebar dropdowns at every level.
  useEffect(() => {
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
  }, []);

  useEffect(() => {
    window.__refreshLabelSidebar = fetchLabels;
    return () => { delete window.__refreshLabelSidebar; };
  }, [fetchLabels]);

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
      out[lvl] = groupByInstrument(out[lvl]);
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

          {/* Dataset (all levels) */}
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

          {/* Import label (all levels) */}
          <div className="sidebar__section">
            <h2 className="sidebar__section-title">
              {level === "patient" ? "Study Import Label" : "Import Label"}
            </h2>
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

          {/* Annotation Labels */}
          <div className="sidebar__section">
            <h2 className="sidebar__section-title">Annotation Labels</h2>
            {(filters.label || Object.keys(labelValues).length > 0) && (
              <button
                onClick={() =>
                  onFilterChange({ label: null, labelLevel: null, labelValues: {} })
                }
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
                    {!isLevelCollapsed && groupedByLevel[lvl].map(({ key, name, items: labels }) => {
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
                                const labelKey = `${lvl}:${l.label}`;
                                const def = defByKey[labelKey];
                                if (def?.datatype === "select") {
                                  const selected = labelValues[labelKey] || [];
                                  return (
                                    <LabelValueFilter
                                      key={labelKey}
                                      label={l.label}
                                      caseCount={l.count}
                                      options={def.options}
                                      selected={selected}
                                      pinned={pinnedKey === labelKey}
                                      onToggleValue={(v) => toggleLabelValue(labelKey, v)}
                                      onClear={() => clearLabelValue(labelKey)}
                                      onTogglePin={() =>
                                        setPinnedKey((cur) => (cur === labelKey ? null : labelKey))
                                      }
                                    />
                                  );
                                }
                                const isActive = filters.label === l.label && filters.labelLevel === lvl;
                                return (
                                  <li
                                    key={labelKey}
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
