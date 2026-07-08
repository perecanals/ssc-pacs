import { useEffect, useCallback, useRef } from "react";
import { apiFetch } from "../api/client";

const SAVE_DEBOUNCE_MS = 800;

// Debounced server-save plumbing shared by usePreferencePersistence and
// useSessionStatePersistence. Returns scheduleSave(); call it when watched
// state changes — the PUT fires after 800 ms of quiet with getBody()'s
// current value. Anything still pending flushes on unmount and on
// beforeunload (keepalive so the request survives tab close). Callers own
// their save gates (first-render skip, hydration, restored-echo suppression).
export default function useDebouncedServerSave({ enabled, path, getBody }) {
  const dirty = useRef(false);
  const timer = useRef(null);
  // Ref-mirrored config keeps scheduleSave/flush identity-stable.
  const cfg = useRef({ enabled, path, getBody });
  cfg.current = { enabled, path, getBody };

  const save = useCallback((opts = {}) => {
    apiFetch(cfg.current.path, {
      method: "PUT",
      body: JSON.stringify(cfg.current.getBody()),
      ...opts,
    }).catch(() => {});
  }, []);

  const scheduleSave = useCallback(() => {
    if (!cfg.current.enabled) return;
    dirty.current = true;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      dirty.current = false;
      save();
    }, SAVE_DEBOUNCE_MS);
  }, [save]);

  const flush = useCallback(() => {
    clearTimeout(timer.current);
    if (!cfg.current.enabled || !dirty.current) return;
    dirty.current = false;
    save({ keepalive: true });
  }, [save]);

  useEffect(() => {
    window.addEventListener("beforeunload", flush);
    return () => {
      window.removeEventListener("beforeunload", flush);
      flush();
    };
  }, [flush]);

  return scheduleSave;
}
