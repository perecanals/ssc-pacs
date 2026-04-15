import { useState, useEffect } from "react";
import { apiGet } from "../api/client";
import "./Sidebar.css";

const MODALITIES = ["CT", "MR", "CR", "US", "DX", "PT", "NM", "XA", "MG", "RF"];
const LEVEL_ORDER = ["patient", "study", "series"];
const LEVEL_LABELS = { patient: "Patient", study: "Study", series: "Series" };

export default function Sidebar({ level, filters, onFilterChange }) {
  const [labelSummary, setLabelSummary] = useState([]);
  const [studyImportLabels, setStudyImportLabels] = useState([]);

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

  const grouped = {};
  for (const l of labelSummary) {
    const lvl = l.level || "series";
    if (!grouped[lvl]) grouped[lvl] = [];
    grouped[lvl].push(l);
  }

  return (
    <aside className="sidebar">
      {/* Annotation Labels */}
      <div>
        <h2 className="sidebar__section-title">
          Annotation Labels
        </h2>
        {filters.label && (
          <button
            onClick={() => onFilterChange({ label: null, labelLevel: null })}
            className="sidebar__clear-filter"
          >
            Clear filter
          </button>
        )}
        {Object.keys(grouped).length === 0 ? (
          <p className="sidebar__empty-msg">
            No annotations yet
          </p>
        ) : (
          LEVEL_ORDER.filter((lvl) => grouped[lvl]).map((lvl) => (
            <div key={lvl} className="sidebar__level-group">
              <div className="sidebar__level-heading">
                {LEVEL_LABELS[lvl]}
              </div>
              <ul className="sidebar__label-list">
                {grouped[lvl].map((l) => {
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
                      <span className="sidebar__label-text">
                        {l.label}
                      </span>
                      <span className="sidebar__label-count">{l.count}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))
        )}
      </div>

      {/* Quick Filters */}
      {level === "patient" && (
        <div className="sidebar__quick-filters">
          <h2 className="sidebar__section-title">
            Quick Filters
          </h2>
          <div className="sidebar__filter-group">
            <label className="sidebar__filter-label" htmlFor="sidebar-study-import-label">
              Study import label
            </label>
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
      {(level === "study" || level === "series") && (
        <div className="sidebar__quick-filters">
          <h2 className="sidebar__section-title">
            Quick Filters
          </h2>
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
    </aside>
  );
}
