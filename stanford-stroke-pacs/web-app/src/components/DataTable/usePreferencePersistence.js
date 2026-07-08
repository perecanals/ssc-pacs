import { useEffect, useRef } from "react";
import useDebouncedServerSave from "../../hooks/useDebouncedServerSave";
import { hasFilterValue } from "../../utils/table";

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
  };

  const scheduleSave = useDebouncedServerSave({
    enabled: !!currentUser,
    path: `/api/preferences/${level}`,
    getBody: () => ({ prefs: latestPrefs.current }),
  });

  useEffect(() => {
    // The mount render carries the restored prefs — saving them back would
    // be a wasted PUT.
    if (initialRender.current) {
      initialRender.current = false;
      return;
    }
    scheduleSave();
  }, [scheduleSave, currentUser, level, visibleKeys, columnOrder, sortBy, sortDir, columnFilters, frozenFirstCol, fontScale, statusColVisible]);
}
