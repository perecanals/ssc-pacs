import { useEffect, useMemo, useRef } from "react";
import useDebouncedServerSave from "../../hooks/useDebouncedServerSave";
import { COLUMN_DEFAULTS_VERSION, hasFilterValue } from "../../utils/table";

export default function usePreferencePersistence({
  currentUser,
  level,
  visibleKeys,
  columnOrder,
  sortBy,
  sortDir,
  columnFilters,
  frozenFirstCol,
  fontScale,
  statusColVisible,
  prefsUpgraded = false,
  allCols = [],
  catalogReady = false,
}) {
  const latestPrefs = useRef({});
  const initialRender = useRef(true);

  // Prefs outlive the columns they name: a retired builtin (femoral_sheath_time,
  // v1.13) or a deleted label leaves a dangling key in every saved pref that
  // selected it. Reading tolerates that — resolution is by lookup, so unknown
  // keys render nothing — but they would otherwise be saved back forever. Drop
  // them on the next save, so prefs self-heal the first time the user touches
  // the table.
  //
  // Guarded on catalogReady: label definitions load async, and pruning against
  // a catalog that has not arrived (or whose fetch failed) would silently
  // delete every label column the user had selected. Null = do not prune.
  //
  // Two namespaces, deliberately: visibleKeys/columnOrder hold a column's full
  // `key` (builtin:patient:stroke_date), while columnFilters is keyed by
  // `sourceKey` for builtins (stroke_date) and by `key` for labels
  // (label:series_type). Pruning either against the other's set would wipe good
  // prefs — see TableHeader's filter inputs.
  const knownColumnKeys = useMemo(
    () => (catalogReady ? new Set(allCols.map((c) => c.key)) : null),
    [catalogReady, allCols],
  );
  const knownFilterKeys = useMemo(
    () =>
      catalogReady
        ? new Set(allCols.map((c) => (c.builtin ? c.sourceKey : c.key)))
        : null,
    [catalogReady, allCols],
  );

  const pruneKeys = (keys) =>
    knownColumnKeys ? keys.filter((k) => knownColumnKeys.has(k)) : keys;

  latestPrefs.current = {
    visibleKeys: pruneKeys(visibleKeys),
    columnOrder: pruneKeys(columnOrder),
    sortBy,
    sortDir,
    columnFilters: Object.fromEntries(
      Object.entries(columnFilters).filter(
        ([k, v]) =>
          hasFilterValue(v) && (!knownFilterKeys || knownFilterKeys.has(k)),
      ),
    ),
    freezeFirstCol: frozenFirstCol,
    fontScale,
    statusColVisible,
    defaultsVersion: COLUMN_DEFAULTS_VERSION,
  };

  // Did pruning actually drop anything? If so the saved prefs are stale on
  // disk, so write the cleaned set once now rather than waiting for the user to
  // happen to touch the table. False until the catalog arrives (nothing is
  // pruned before then), which is what makes it flip and re-fire the effect.
  const activeFilterCount =
    Object.values(columnFilters).filter(hasFilterValue).length;
  const prefsPruned =
    latestPrefs.current.visibleKeys.length !== visibleKeys.length ||
    latestPrefs.current.columnOrder.length !== columnOrder.length ||
    Object.keys(latestPrefs.current.columnFilters).length !== activeFilterCount;

  const scheduleSave = useDebouncedServerSave({
    enabled: !!currentUser,
    path: `/api/preferences/${level}`,
    getBody: () => ({ prefs: latestPrefs.current }),
  });

  useEffect(() => {
    // The mount render carries the restored prefs — saving them back would be a
    // wasted PUT, unless useColumnPrefs just merged newly-introduced columns
    // (the bumped marker has to land now, or the merge repeats on every load and
    // resurrects a column the user hid) or pruning found dead keys to drop.
    if (initialRender.current) {
      initialRender.current = false;
      if (!prefsUpgraded && !prefsPruned) return;
    }
    scheduleSave();
  }, [
    scheduleSave,
    prefsUpgraded,
    prefsPruned,
    currentUser,
    level,
    visibleKeys,
    columnOrder,
    sortBy,
    sortDir,
    columnFilters,
    frozenFirstCol,
    fontScale,
    statusColVisible,
  ]);
}
