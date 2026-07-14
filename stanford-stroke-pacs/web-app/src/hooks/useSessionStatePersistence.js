import { useEffect, useState, useRef } from "react";
import { apiGet } from "../api/client";
import useDebouncedServerSave from "./useDebouncedServerSave";

const VALID_LEVELS = ["patient", "study", "series"];

// Rebuild a clean { key: string[] } map from stored session data, dropping
// anything malformed. Backs both multi-value sidebar quick filters: annotation
// select labels (keyed "<level>:<label>") and the Auto columns (keyed by field).
function sanitizeValueMap(stored, keyIsValid) {
  const out = {};
  if (!stored || typeof stored !== "object") return out;
  for (const [key, vals] of Object.entries(stored)) {
    if (typeof key !== "string" || !keyIsValid(key) || !Array.isArray(vals))
      continue;
    const clean = [
      ...new Set(
        vals
          .filter((v) => typeof v === "string" && v.trim())
          .map((v) => v.trim()),
      ),
    ];
    if (clean.length) out[key] = clean;
  }
  return out;
}

const AUTO_FILTER_FIELDS = ["series_type", "timepoint"];

// Validates a stored session blob against the live filter shape. Only the
// keys present in `defaultFilters` are kept, only string values are
// accepted (labelValues is the one structured exception), and filters that
// the Sidebar cannot render at the restored level are dropped (an invisible
// filter would still constrain the table).
function sanitizeSession(session, defaultFilters) {
  const level = VALID_LEVELS.includes(session?.level)
    ? session.level
    : "patient";
  const filters = { ...defaultFilters };
  const stored = session?.filters;
  if (stored && typeof stored === "object") {
    for (const key of Object.keys(defaultFilters)) {
      if (key === "labelValues") {
        filters.labelValues = sanitizeValueMap(stored.labelValues, (k) =>
          k.includes(":"),
        );
      } else if (key === "autoValues") {
        filters.autoValues = sanitizeValueMap(stored.autoValues, (k) =>
          AUTO_FILTER_FIELDS.includes(k),
        );
      } else if (typeof stored[key] === "string") {
        filters[key] = stored[key];
      }
    }
  }
  // Dataset + import-label dropdowns now render at every level; only Modality is
  // patient-inapplicable. Keep dataset/studyImportLabel across all levels.
  if (level === "patient") {
    filters.modality = null;
  }
  // Preview-pane height in px; null = the CSS default. Clamped to the drag
  // bounds' floor (the CSS max-height caps rendering per-viewport).
  const rawHeight = Number(session?.previewHeight);
  const previewHeight = Number.isFinite(rawHeight)
    ? Math.min(4000, Math.max(320, Math.round(rawHeight)))
    : null;
  // Sidebar visibility; anything but an explicit false means open.
  const sidebarOpen = session?.sidebarOpen !== false;
  // Key order must match latestSession.current — the restored-echo check
  // compares JSON strings.
  return { level, filters, previewHeight, sidebarOpen };
}

// Persists the Navigator's session state (current hierarchy level + sidebar
// quick filters + sidebar visibility + preview-pane height) under the
// `_global` preferences level, and restores it on mount. The PUT owns the
// entire `_global` prefs row — if another consumer ever stores state there,
// this must merge, not replace.
export default function useSessionStatePersistence({
  ready,
  currentUser,
  level,
  filters,
  previewHeight,
  sidebarOpen,
  defaultFilters,
}) {
  const [restored, setRestored] = useState(null);
  const latestSession = useRef(null);
  const hydrated = useRef(false);
  const restoredJson = useRef(null);

  latestSession.current = { level, filters, previewHeight, sidebarOpen };

  useEffect(() => {
    // Wait for the auth probe to settle so the GET fires once, as the
    // right user — not first anonymously and again after /api/me resolves.
    if (!ready) return undefined;
    let cancelled = false;
    hydrated.current = false;
    setRestored(null);
    apiGet("/api/preferences/_global")
      .then((data) => data.prefs?.session)
      .catch(() => null)
      .then((session) => {
        if (cancelled) return;
        const sanitized = sanitizeSession(session, defaultFilters);
        setRestored(sanitized);
        restoredJson.current = JSON.stringify(sanitized);
        hydrated.current = true;
      });
    return () => {
      cancelled = true;
    };
    // defaultFilters is a module-level constant in the caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, currentUser]);

  const scheduleSave = useDebouncedServerSave({
    enabled: !!currentUser,
    path: "/api/preferences/_global",
    getBody: () => ({ prefs: { session: latestSession.current } }),
  });

  useEffect(() => {
    // Hydration gate (not just first-render): a save fired before the GET
    // resolves would clobber the stored state with defaults.
    if (!hydrated.current || !currentUser) return;
    // The caller seeding its state from the restored values re-triggers this
    // effect once; writing the data we just read back would be a wasted PUT.
    if (
      restoredJson.current !== null &&
      JSON.stringify(latestSession.current) === restoredJson.current
    ) {
      restoredJson.current = null;
      return;
    }
    restoredJson.current = null;
    scheduleSave();
  }, [scheduleSave, currentUser, level, filters, previewHeight, sidebarOpen]);

  return {
    loaded: restored !== null,
    restoredLevel: restored?.level,
    restoredFilters: restored?.filters,
    restoredPreviewHeight: restored?.previewHeight ?? null,
    restoredSidebarOpen: restored?.sidebarOpen ?? true,
  };
}
