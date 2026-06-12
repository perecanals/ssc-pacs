import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import PropTypes from "prop-types";
import {
  compareLabelDefsDefault,
  DEFAULT_VISIBLE_LABEL_NAMES,
  LEVEL_LABELS,
  LEVEL_ORDER,
  LEVEL_RANK,
} from "../utils/table";
import "./ColumnSelector.css";

const COLUMN_KEY_MIGRATIONS = {
  integration_id: "import_id",
};

const UNASSIGNED = "__unassigned__";

export function useColumnPrefs(labelDefs, builtinCols, level, initialPrefs = {}) {
  const builtinKeyAliases = useMemo(() => {
    const aliases = new Map();
    for (const col of builtinCols) {
      for (const alias of col.legacyKeys || []) aliases.set(alias, col.key);
    }
    return aliases;
  }, [builtinCols]);
  const defaultBuiltinKeys = useMemo(
    () => builtinCols
      .filter((c) => c.defaultVisible !== false)
      .map((c) => c.key),
    [builtinCols],
  );
  const builtinColsFull = useMemo(
    () => builtinCols.map((c) => ({ ...c, builtin: true })),
    [builtinCols],
  );
  const labelCols = useMemo(
    () => [...labelDefs].sort(compareLabelDefsDefault).map((d) => ({
      key: `label:${d.name}`,
      label: d.name,
      builtin: false,
      level: d.level,
      datatype: d.datatype,
      description: d.description,
      options: d.options || [],
      instrument: d.instrument || null,
      labelDef: d,
    })),
    [labelDefs],
  );
  const allCols = useMemo(
    () => [...builtinColsFull, ...labelCols],
    [builtinColsFull, labelCols],
  );

  // Label columns that are on by default (same level rule as builtins: a
  // label applies at the active level or below). Label defs load async, so
  // these are merged into the visible set when they arrive (below) rather
  // than in the useState initializer.
  const defaultLabelKeys = useMemo(
    () => labelCols
      .filter((c) =>
        DEFAULT_VISIBLE_LABEL_NAMES.includes(c.label) &&
        LEVEL_RANK[c.level || "series"] >= LEVEL_RANK[level])
      .map((c) => c.key),
    [labelCols, level],
  );

  const migrateKeys = useCallback((keys) =>
    Array.from(
      new Set(
        (Array.isArray(keys) ? keys : [])
          .map((key) => {
            const migrated = COLUMN_KEY_MIGRATIONS[key] || key;
            return builtinKeyAliases.get(migrated) || migrated;
          }),
      ),
    ), [builtinKeyAliases]);

  const hasSavedPrefs =
    Array.isArray(initialPrefs.visibleKeys) && initialPrefs.visibleKeys.length > 0;

  const [visibleKeys, setVisibleKeys] = useState(() => {
    if (hasSavedPrefs) {
      return migrateKeys(initialPrefs.visibleKeys);
    }
    return defaultBuiltinKeys;
  });

  // When showing defaults (no saved prefs), add the default-on label columns
  // once their definitions arrive from the async fetch. Applied a single
  // time so a user hiding one of them afterwards is not overridden by a
  // later labelDefs refetch (e.g. after creating a new label).
  const appliedDefaultLabelsRef = useRef(hasSavedPrefs);
  useEffect(() => {
    if (appliedDefaultLabelsRef.current || defaultLabelKeys.length === 0) return;
    appliedDefaultLabelsRef.current = true;
    setVisibleKeys((prev) => Array.from(new Set([...prev, ...defaultLabelKeys])));
  }, [defaultLabelKeys]);

  const unorderedVisibleCols = allCols.filter((c) => visibleKeys.includes(c.key));

  const toggle = (key) => {
    setVisibleKeys((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  };

  const setKeysVisible = useCallback((keys, visible) => {
    setVisibleKeys((prev) => {
      if (visible) return Array.from(new Set([...prev, ...keys]));
      const drop = new Set(keys);
      return prev.filter((k) => !drop.has(k));
    });
  }, []);

  const [columnOrder, setColumnOrder] = useState(() => {
    if (Array.isArray(initialPrefs.columnOrder) && initialPrefs.columnOrder.length > 0) {
      return migrateKeys(initialPrefs.columnOrder);
    }
    return [];
  });

  const visibleCols = useMemo(() => {
    if (columnOrder.length === 0) return unorderedVisibleCols;
    const orderMap = new Map(columnOrder.map((k, i) => [k, i]));
    return [...unorderedVisibleCols].sort((a, b) =>
      (orderMap.get(a.key) ?? Infinity) - (orderMap.get(b.key) ?? Infinity)
    );
  }, [unorderedVisibleCols, columnOrder]);

  const reorder = useCallback((fromKey, toKey, side) => {
    if (fromKey === toKey) return;
    setColumnOrder(() => {
      const keys = visibleCols.map((c) => c.key);
      const fromIdx = keys.indexOf(fromKey);
      if (fromIdx === -1) return keys;
      keys.splice(fromIdx, 1);
      let insertIdx = keys.indexOf(toKey);
      if (insertIdx === -1) return [...keys, fromKey];
      if (side === "after") insertIdx += 1;
      keys.splice(insertIdx, 0, fromKey);
      return keys;
    });
  }, [visibleCols]);

  const resetColumns = useCallback(() => {
    setVisibleKeys(Array.from(new Set([...defaultBuiltinKeys, ...defaultLabelKeys])));
    setColumnOrder([]);
  }, [defaultBuiltinKeys, defaultLabelKeys]);

  return {
    allCols, visibleCols, visibleKeys, columnOrder,
    toggle, setKeysVisible, reorder, resetColumns,
  };
}

function groupLabelsByInstrument(labels) {
  const groups = new Map();
  for (const c of labels) {
    const key = c.instrument || UNASSIGNED;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  return Array.from(groups.entries())
    .map(([key, cols]) => ({
      key,
      name: key === UNASSIGNED ? "Unassigned" : key,
      cols,
    }))
    .sort((a, b) => {
      if (a.key === UNASSIGNED) return 1;
      if (b.key === UNASSIGNED) return -1;
      if (b.cols.length !== a.cols.length) return b.cols.length - a.cols.length;
      return a.name.localeCompare(b.name);
    });
}

export default function ColumnSelector({
  allCols,
  visibleKeys,
  onToggle,
  onSetKeysVisible,
  onEditLabel,
  showStatusColumn = false,
  statusColumnVisible = true,
  onToggleStatusColumn,
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const close = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);

  const builtins = allCols.filter((c) => c.builtin);
  const labels = allCols.filter((c) => !c.builtin);
  const groupByLevel = (cols) =>
    LEVEL_ORDER
      .map((lvl) => ({
        lvl,
        cols: cols.filter((c) => (c.level || "series") === lvl),
      }))
      .filter(({ cols }) => cols.length > 0);

  const builtinGroups = groupByLevel(builtins);
  const instrumentGroups = useMemo(() => groupLabelsByInstrument(labels), [labels]);

  const visibleSet = useMemo(() => new Set(visibleKeys), [visibleKeys]);

  return (
    <div className="col-selector" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="pill-btn col-selector__trigger"
      >
        Displayed Columns &#9662;
      </button>
      {open && (
        <div className="col-selector__dropdown">
          <div className="col-selector__section-title">
            Data columns
          </div>
          {builtinGroups.map(({ lvl, cols }, idx) => (
            <div key={`builtin-${lvl}`}>
              {idx > 0 && <div className="col-selector__divider" />}
              <div className="col-selector__subgroup-title">
                {LEVEL_LABELS[lvl] || lvl} columns
              </div>
              {cols.map((c) => (
                <label key={c.key} className="col-selector__item">
                  <input
                    type="checkbox"
                    checked={visibleSet.has(c.key)}
                    onChange={() => onToggle(c.key)}
                  />
                  {c.label}
                </label>
              ))}
              {showStatusColumn && lvl === "patient" && (
                <label className="col-selector__item">
                  <input
                    type="checkbox"
                    checked={statusColumnVisible}
                    onChange={onToggleStatusColumn}
                  />
                  Status
                </label>
              )}
            </div>
          ))}
          <div className="col-selector__divider" />
          <div className="col-selector__section-title">
            Annotation labels
          </div>
          {instrumentGroups.length === 0 && (
            <div className="col-selector__empty">No labels defined yet.</div>
          )}
          {instrumentGroups.map(({ key, name, cols }) => {
            const keys = cols.map((c) => c.key);
            const visibleCount = keys.filter((k) => visibleSet.has(k)).length;
            const allVisible = visibleCount === keys.length;
            return (
              <div key={`instr-${key}`}>
                <div className="col-selector__instrument-header">
                  <span className="col-selector__instrument-name">
                    {name}{" "}
                    <span className="col-selector__instrument-count">
                      ({cols.length})
                    </span>
                  </span>
                  <button
                    type="button"
                    onClick={() => onSetKeysVisible(keys, !allVisible)}
                    className="col-selector__bulk-btn"
                  >
                    {allVisible ? "Hide all" : "Show all"}
                  </button>
                </div>
                {cols.map((c) => (
                  <div key={c.key} className="col-selector__item-row">
                    <label
                      title={c.description || ""}
                      className="col-selector__item col-selector__item--flex"
                    >
                      <input
                        type="checkbox"
                        checked={visibleSet.has(c.key)}
                        onChange={() => onToggle(c.key)}
                      />
                      <span className="col-selector__label-text">
                        {c.label}{" "}
                        <span className="col-selector__datatype">
                          ({c.datatype} · {c.level})
                        </span>
                      </span>
                    </label>
                    {onEditLabel && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onEditLabel(c.labelDef);
                        }}
                        title="Edit label"
                        className="col-selector__edit-btn"
                      >
                        {"✎"}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

ColumnSelector.propTypes = {
  allCols: PropTypes.array.isRequired,
  visibleKeys: PropTypes.arrayOf(PropTypes.string).isRequired,
  onToggle: PropTypes.func.isRequired,
  onSetKeysVisible: PropTypes.func.isRequired,
  onEditLabel: PropTypes.func,
  showStatusColumn: PropTypes.bool,
  statusColumnVisible: PropTypes.bool,
  onToggleStatusColumn: PropTypes.func,
};
