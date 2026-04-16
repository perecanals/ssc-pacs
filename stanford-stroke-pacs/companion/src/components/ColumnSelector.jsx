import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import PropTypes from "prop-types";
import "./ColumnSelector.css";

const COLUMN_KEY_MIGRATIONS = {
  integration_id: "import_id",
};

const LEVEL_LABELS = { patient: "Patient", study: "Study", series: "Series" };
const LEVEL_ORDER = ["patient", "study", "series"];

export function useColumnPrefs(labelDefs, builtinCols, level, forcedVisibleKeys = [], initialPrefs = {}) {
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
    () => labelDefs.map((d) => ({
      key: `label:${d.name}`,
      label: d.name,
      builtin: false,
      level: d.level,
      datatype: d.datatype,
      description: d.description,
      options: d.options || [],
    })),
    [labelDefs],
  );
  const allCols = useMemo(
    () => [...builtinColsFull, ...labelCols],
    [builtinColsFull, labelCols],
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

  const [visibleKeys, setVisibleKeys] = useState(() => {
    if (Array.isArray(initialPrefs.visibleKeys) && initialPrefs.visibleKeys.length > 0) {
      return migrateKeys(initialPrefs.visibleKeys);
    }
    return defaultBuiltinKeys;
  });

  const effectiveVisibleKeys = Array.from(
    new Set([...visibleKeys, ...forcedVisibleKeys]),
  );
  const unorderedVisibleCols = allCols.filter((c) => effectiveVisibleKeys.includes(c.key));

  const toggle = (key) => {
    setVisibleKeys((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  };

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
    setVisibleKeys(defaultBuiltinKeys);
    setColumnOrder([]);
  }, [defaultBuiltinKeys]);

  return { allCols, visibleCols, visibleKeys, columnOrder, effectiveVisibleKeys, toggle, reorder, resetColumns };
}

export default function ColumnSelector({ allCols, visibleKeys, onToggle }) {
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
  const labelGroups = groupByLevel(labels);

  return (
    <div className="col-selector" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="col-selector__trigger"
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
                    checked={visibleKeys.includes(c.key)}
                    onChange={() => onToggle(c.key)}
                  />
                  {c.label}
                </label>
              ))}
            </div>
          ))}
          <div className="col-selector__divider" />
          <div className="col-selector__section-title">
            Annotation labels
          </div>
          {labelGroups.map(({ lvl, cols }) => (
            <div key={`label-${lvl}`}>
              <div className="col-selector__subgroup-title">
                {LEVEL_LABELS[lvl] || lvl} labels
              </div>
              {cols.map((c) => (
                <label
                  key={c.key}
                  title={c.description || ""}
                  className="col-selector__item"
                >
                  <input
                    type="checkbox"
                    checked={visibleKeys.includes(c.key)}
                    onChange={() => onToggle(c.key)}
                  />
                  {c.label}{" "}
                  <span className="col-selector__datatype">
                    ({c.datatype})
                  </span>
                </label>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

ColumnSelector.propTypes = {
  allCols: PropTypes.array.isRequired,
  visibleKeys: PropTypes.arrayOf(PropTypes.string).isRequired,
  onToggle: PropTypes.func.isRequired,
};
