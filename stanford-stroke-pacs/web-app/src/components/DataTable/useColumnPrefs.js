import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import {
  compareLabelDefsDefault,
  DEFAULT_VISIBLE_LABEL_NAMES,
  LEVEL_RANK,
} from "../../utils/table";

// Deduped copy of a saved key array (duplicate keys would collide as React keys).
const sanitizeKeys = (keys) =>
  Array.from(new Set(Array.isArray(keys) ? keys : []));

// Column visibility/order state for the DataTable, seeded from the saved
// server prefs and merged with the label-definition catalog as it loads.
export default function useColumnPrefs(
  labelDefs,
  builtinCols,
  level,
  initialPrefs = {},
) {
  const defaultBuiltinKeys = useMemo(
    () =>
      builtinCols.filter((c) => c.defaultVisible !== false).map((c) => c.key),
    [builtinCols],
  );
  const builtinColsFull = useMemo(
    () => builtinCols.map((c) => ({ ...c, builtin: true })),
    [builtinCols],
  );
  const labelCols = useMemo(
    () =>
      [...labelDefs].sort(compareLabelDefsDefault).map((d) => ({
        key: `label:${d.name}`,
        label: d.name,
        builtin: false,
        level: d.level,
        datatype: d.datatype,
        description: d.description,
        options: d.options || [],
        instrument: d.instrument || null,
        created_at: d.created_at,
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
    () =>
      labelCols
        .filter(
          (c) =>
            DEFAULT_VISIBLE_LABEL_NAMES.includes(c.label) &&
            LEVEL_RANK[c.level || "series"] >= LEVEL_RANK[level],
        )
        .map((c) => c.key),
    [labelCols, level],
  );

  const hasSavedPrefs =
    Array.isArray(initialPrefs.visibleKeys) &&
    initialPrefs.visibleKeys.length > 0;

  // Builtin columns introduced since the user's prefs were last stamped. Merged
  // in exactly once — usePreferencePersistence writes the new marker straight
  // away, so a user who then hides one of them keeps it hidden.
  const savedDefaultsVersion = Number(initialPrefs.defaultsVersion) || 0;
  const pendingDefaultKeys = useMemo(
    () =>
      builtinCols
        .filter(
          (c) =>
            (c.introducedIn ?? 0) > savedDefaultsVersion &&
            c.defaultVisible !== false,
        )
        .map((c) => c.key),
    [builtinCols, savedDefaultsVersion],
  );
  const prefsUpgraded = hasSavedPrefs && pendingDefaultKeys.length > 0;

  const [visibleKeys, setVisibleKeys] = useState(() => {
    if (hasSavedPrefs) {
      return Array.from(
        new Set([
          ...sanitizeKeys(initialPrefs.visibleKeys),
          ...pendingDefaultKeys,
        ]),
      );
    }
    return defaultBuiltinKeys;
  });

  // When showing defaults (no saved prefs), add the default-on label columns
  // once their definitions arrive from the async fetch. Applied a single
  // time so a user hiding one of them afterwards is not overridden by a
  // later labelDefs refetch (e.g. after creating a new label).
  const appliedDefaultLabelsRef = useRef(hasSavedPrefs);
  useEffect(() => {
    if (appliedDefaultLabelsRef.current || defaultLabelKeys.length === 0)
      return;
    appliedDefaultLabelsRef.current = true;
    setVisibleKeys((prev) =>
      Array.from(new Set([...prev, ...defaultLabelKeys])),
    );
  }, [defaultLabelKeys]);

  const unorderedVisibleCols = allCols.filter((c) =>
    visibleKeys.includes(c.key),
  );

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

  const [columnOrder, setColumnOrder] = useState(() =>
    sanitizeKeys(initialPrefs.columnOrder),
  );

  const visibleCols = useMemo(() => {
    if (columnOrder.length === 0) return unorderedVisibleCols;
    const orderMap = new Map(columnOrder.map((k, i) => [k, i]));
    return [...unorderedVisibleCols].sort(
      (a, b) =>
        (orderMap.get(a.key) ?? Infinity) - (orderMap.get(b.key) ?? Infinity),
    );
  }, [unorderedVisibleCols, columnOrder]);

  const reorder = useCallback(
    (fromKey, toKey, side) => {
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
    },
    [visibleCols],
  );

  const resetColumns = useCallback(() => {
    setVisibleKeys(
      Array.from(new Set([...defaultBuiltinKeys, ...defaultLabelKeys])),
    );
    setColumnOrder([]);
  }, [defaultBuiltinKeys, defaultLabelKeys]);

  return {
    allCols,
    visibleCols,
    visibleKeys,
    columnOrder,
    prefsUpgraded,
    toggle,
    setKeysVisible,
    reorder,
    resetColumns,
  };
}
