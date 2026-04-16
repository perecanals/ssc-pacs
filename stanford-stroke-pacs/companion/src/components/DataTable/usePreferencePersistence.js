import { useEffect, useCallback, useRef } from "react";
import { apiFetch } from "../../api/client";
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
}) {
  const latestPrefs = useRef({});
  const dirty = useRef(false);
  const saveTimer = useRef(null);
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
  };

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
}
