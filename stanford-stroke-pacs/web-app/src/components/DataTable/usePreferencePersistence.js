import { useEffect, useRef } from "react";
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
}) {
  const latestPrefs = useRef({});
  const initialRender = useRef(true);

  latestPrefs.current = {
    visibleKeys,
    columnOrder,
    sortBy,
    sortDir,
    columnFilters: Object.fromEntries(
      Object.entries(columnFilters).filter(([, v]) => hasFilterValue(v)),
    ),
    freezeFirstCol: frozenFirstCol,
    fontScale,
    statusColVisible,
    defaultsVersion: COLUMN_DEFAULTS_VERSION,
  };

  const scheduleSave = useDebouncedServerSave({
    enabled: !!currentUser,
    path: `/api/preferences/${level}`,
    getBody: () => ({ prefs: latestPrefs.current }),
  });

  useEffect(() => {
    // The mount render carries the restored prefs — saving them back would be a
    // wasted PUT, unless useColumnPrefs just merged newly-introduced columns:
    // the bumped marker has to land now, or the merge repeats on every load and
    // resurrects a column the user hid.
    if (initialRender.current) {
      initialRender.current = false;
      if (!prefsUpgraded) return;
    }
    scheduleSave();
  }, [
    scheduleSave,
    prefsUpgraded,
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
