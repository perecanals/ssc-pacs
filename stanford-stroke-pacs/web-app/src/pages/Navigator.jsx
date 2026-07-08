import { useState, useCallback, useEffect, useRef } from "react";
import { getStorageMode, resolveOhifViewerUrl } from "../api/warmOhif";
import { useAuth } from "../context/AuthContext";
import useSessionStatePersistence from "../hooks/useSessionStatePersistence";
import TopBar from "../components/TopBar";
import Sidebar from "../components/Sidebar";
import DataTable from "../components/DataTable";
import PreviewPane from "../components/PreviewPane";
import "./Navigator.css";

const LEVELS = [
  { key: "patient", label: "Patients" },
  { key: "study", label: "Studies" },
  { key: "series", label: "Series" },
];

export const DEFAULT_FILTERS = {
  label: null,
  labelLevel: null,
  patientId: null,
  modality: null,
  description: null,
  studyImportLabel: null,
  dataset: null,
  // Sidebar select-value quick filters: { "<level>:<label>": ["v1", "v2"] }.
  // Merged into the `label_filters` request param by useTableData.
  labelValues: {},
};

export default function Navigator() {
  const { loading: authLoading, currentUser } = useAuth();
  const [level, setLevel] = useState("patient");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const toggleSidebar = useCallback(() => setSidebarOpen((prev) => !prev), []);
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  // null = the CSS default height; a number once the user drag-resizes.
  const [previewHeight, setPreviewHeight] = useState(null);

  // Restore last session's level + sidebar filters/visibility + preview-pane
  // height from the `_global` preferences bucket; saves them back (debounced)
  // whenever they change.
  const [sessionLoaded, setSessionLoaded] = useState(false);
  const {
    loaded: restoreLoaded,
    restoredLevel,
    restoredFilters,
    restoredPreviewHeight,
    restoredSidebarOpen,
  } = useSessionStatePersistence({
    ready: !authLoading,
    currentUser,
    level,
    filters,
    previewHeight,
    sidebarOpen,
    defaultFilters: DEFAULT_FILTERS,
  });
  useEffect(() => {
    if (!restoreLoaded) return;
    setLevel(restoredLevel);
    setFilters(restoredFilters);
    setPreviewHeight(restoredPreviewHeight);
    setSidebarOpen(restoredSidebarOpen);
    setSessionLoaded(true);
  }, [restoreLoaded, restoredLevel, restoredFilters, restoredPreviewHeight, restoredSidebarOpen]);

  // Bumped when the DataTable mutates annotations so the Sidebar refetches
  // its label summary/definitions (counts + new select values).
  const [labelsNonce, setLabelsNonce] = useState(0);
  const handleLabelsMutated = useCallback(() => setLabelsNonce((n) => n + 1), []);

  const [previewSelection, setPreviewSelection] = useState(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoadingLabel, setPreviewLoadingLabel] = useState("");
  const [toolbarHostEl, setToolbarHostEl] = useState(null);
  const previewRequestRef = useRef(0);

  const clearPreview = useCallback(() => {
    previewRequestRef.current += 1;
    setPreviewSelection(null);
    setPreviewUrl("");
    setPreviewLoading(false);
    setPreviewError("");
    setPreviewOpen(false);
  }, []);

  const handleFilterChange = useCallback((patch) => {
    setFilters((prev) => ({ ...prev, ...patch }));
  }, []);

  // Clears every sidebar quick filter (label, modality, study-import
  // label). Paired with the DataTable's column-filter reset behind the
  // single "Reset Filters" toolbar button. The DataTable's data hook
  // resets the accumulated list (and scrolls to top) whenever filters
  // change, so no page bookkeeping is needed here.
  const handleResetSidebarFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS);
  }, []);

  const handleLevelChange = useCallback((newLevel) => {
    setLevel(newLevel);
    setFilters(DEFAULT_FILTERS);
    clearPreview();
  }, [clearPreview]);

  const handlePreviewSelect = useCallback(async (selection) => {
    if (!selection?.studyinstanceuid) return;

    if (previewSelection?.rowKey === selection.rowKey) {
      if (selection.sourceLevel === "study") {
        setPreviewSelection(selection);
        return;
      }

      setPreviewSelection(selection);
      if (previewOpen) {
        previewRequestRef.current += 1;
        setPreviewLoading(false);
        setPreviewError("");
        setPreviewOpen(false);
        return;
      }
      if (previewUrl || previewError) {
        setPreviewOpen(true);
        return;
      }
    }

    const requestId = previewRequestRef.current + 1;
    previewRequestRef.current = requestId;

    setPreviewSelection(selection);
    setPreviewOpen(true);
    setPreviewLoading(true);
    setPreviewError("");
    setPreviewLoadingLabel("Checking storage…");

    const params = new URLSearchParams();
    if (selection.seriesinstanceuid) {
      params.set("seriesinstanceuid", selection.seriesinstanceuid);
    }

    try {
      const mode = await getStorageMode();
      if (previewRequestRef.current !== requestId) return;
      if (mode === "cold_path_cache") {
        setPreviewLoadingLabel("Warming imaging cache…");
      } else {
        setPreviewLoadingLabel("Resolving OHIF preview…");
      }
      const url = await resolveOhifViewerUrl(
        selection.studyinstanceuid,
        selection.seriesinstanceuid || null,
      );
      if (previewRequestRef.current !== requestId) return;
      setPreviewUrl(url || "");
    } catch (e) {
      if (previewRequestRef.current !== requestId) return;
      setPreviewUrl("");
      setPreviewError(e?.message || "Could not resolve the OHIF preview for this row.");
    } finally {
      if (previewRequestRef.current === requestId) {
        setPreviewLoading(false);
        setPreviewLoadingLabel("");
      }
    }
  }, [previewError, previewOpen, previewSelection, previewUrl]);

  // Gate rendering until the session restore resolves so the DataTable
  // (keyed by level) mounts exactly once, at the restored level with the
  // restored filters — no fetch-with-defaults-then-refetch.
  if (authLoading || !sessionLoaded) return null;

  return (
    <div className="navigator">
      <TopBar
        levels={LEVELS}
        level={level}
        onLevelChange={handleLevelChange}
        toolbarHostRef={setToolbarHostEl}
      />
      <div className={`navigator__layout${sidebarOpen ? "" : " navigator__layout--sidebar-closed"}`}>
        <Sidebar
          level={level}
          filters={filters}
          onFilterChange={handleFilterChange}
          open={sidebarOpen}
          onToggle={toggleSidebar}
          labelsRefreshNonce={labelsNonce}
        />
        <main className="navigator__main">
          <div className="navigator__content">
            <DataTable
              key={level}
              level={level}
              filters={filters}
              onResetSidebarFilters={handleResetSidebarFilters}
              onPreviewSelect={handlePreviewSelect}
              activeRowKey={previewSelection?.rowKey || null}
              toolbarPortalTarget={toolbarHostEl}
              previewOpen={previewOpen}
              previewUrl={previewUrl}
              onPreviewClose={() => setPreviewOpen(false)}
              onLabelsMutated={handleLabelsMutated}
            />
            <PreviewPane
              selection={previewSelection}
              previewUrl={previewUrl}
              loading={previewLoading}
              loadingLabel={previewLoadingLabel}
              error={previewError}
              isOpen={previewOpen}
              height={previewHeight}
              onHeightChange={setPreviewHeight}
            />
          </div>
        </main>
      </div>
    </div>
  );
}
