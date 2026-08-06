import { useState, useCallback, useEffect, useRef } from "react";
import { getStorageMode, resolveOhifViewerUrl } from "../api/warmOhif";
import { useAuth } from "../context/AuthContext";
import useSessionStatePersistence from "../hooks/useSessionStatePersistence";
import useSecondScreenViewer from "../hooks/useSecondScreenViewer";
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
  // Sidebar quick filters for the machine-derived columns:
  // { series_type: ["NCCT"], timepoint: ["BL", "FU"] }. Sent as repeated query
  // params, which the API ORs. Applies at every level.
  autoValues: {},
};

export default function Navigator() {
  const { loading: authLoading, currentUser } = useAuth();
  const [level, setLevel] = useState("patient");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const toggleSidebar = useCallback(() => setSidebarOpen((prev) => !prev), []);
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  // null = the CSS default height; a number once the user drag-resizes.
  const [previewHeight, setPreviewHeight] = useState(null);
  // Owned here, not inside PreviewPane, so the DataTable footer's Fullscreen
  // button can reach the pane's DOM node.
  const previewPaneRef = useRef(null);

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
  }, [
    restoreLoaded,
    restoredLevel,
    restoredFilters,
    restoredPreviewHeight,
    restoredSidebarOpen,
  ]);

  // Bumped when the DataTable mutates annotations so the Sidebar refetches
  // its label summary/definitions (counts + new select values).
  const [labelsNonce, setLabelsNonce] = useState(0);
  const handleLabelsMutated = useCallback(
    () => setLabelsNonce((n) => n + 1),
    [],
  );

  const [previewSelection, setPreviewSelection] = useState(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoadingLabel, setPreviewLoadingLabel] = useState("");
  const [toolbarHostEl, setToolbarHostEl] = useState(null);
  const previewRequestRef = useRef(0);

  const handlePreviewFullscreen = useCallback(() => {
    previewPaneRef.current?.requestFullscreen?.()?.catch(() => {});
  }, []);

  const secondScreen = useSecondScreenViewer();

  const handleOpenSecondScreen = useCallback(async () => {
    const opened = await secondScreen.open(previewUrl);
    if (opened) {
      setPreviewOpen(false);
      // The popup owns the viewer now. Drop the pane's hidden iframe so it
      // neither holds a superseded study in memory nor re-downloads the next
      // one in the background as row clicks re-route to the popup.
      setPreviewUrl("");
    }
  }, [secondScreen, previewUrl]);

  // Never leave the browser fullscreen on a pane that has been collapsed or
  // whose selection is gone — the user would be staring at a blank screen with
  // no visible way back.
  useEffect(() => {
    if ((!previewOpen || !previewSelection) && document.fullscreenElement) {
      document.exitFullscreen?.()?.catch(() => {});
    }
  }, [previewOpen, previewSelection]);

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

  const handleLevelChange = useCallback(
    (newLevel) => {
      setLevel(newLevel);
      setFilters(DEFAULT_FILTERS);
      clearPreview();
    },
    [clearPreview],
  );

  const handlePreviewSelect = useCallback(
    async (selection) => {
      if (!selection?.studyinstanceuid) return;

      // A row's "Pane" button (forcePane) always lands in the pane: it
      // overrides second-screen routing and never toggles an open pane shut.
      // Plain row clicks keep their toggle behavior.
      const toSecondScreen = !selection.forcePane && secondScreen.isLive();

      if (!toSecondScreen && previewSelection?.rowKey === selection.rowKey) {
        if (selection.sourceLevel === "study" && !selection.forcePane) {
          setPreviewSelection(selection);
          return;
        }

        setPreviewSelection(selection);
        if (previewOpen && !selection.forcePane) {
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

      // While the second-screen popup is live, row clicks re-point it instead
      // of opening the pane. Same request-guard discipline as the pane path;
      // progress/errors surface in the footer chip via secondScreen.status.
      if (toSecondScreen) {
        setPreviewOpen(false);
        setPreviewError("");
        secondScreen.setStatus("Checking storage…");
        try {
          const mode = await getStorageMode();
          if (previewRequestRef.current !== requestId) return;
          secondScreen.setStatus(
            mode === "cold_path_cache"
              ? "Warming imaging cache…"
              : "Resolving OHIF preview…",
          );
          const url = await resolveOhifViewerUrl(
            selection.studyinstanceuid,
            selection.seriesinstanceuid || null,
          );
          if (previewRequestRef.current !== requestId) return;
          // navigate() is false only when the popup died mid-flight (the
          // liveness poll clears `active` within a second) or the URL is
          // empty — don't surprise the user with a pane; the next click
          // routes normally.
          secondScreen.navigate(url);
          secondScreen.setStatus("");
        } catch (e) {
          if (previewRequestRef.current !== requestId) return;
          secondScreen.setStatus(
            e?.message || "Could not resolve the OHIF preview for this row.",
          );
        }
        return;
      }

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
        setPreviewError(
          e?.message || "Could not resolve the OHIF preview for this row.",
        );
      } finally {
        if (previewRequestRef.current === requestId) {
          setPreviewLoading(false);
          setPreviewLoadingLabel("");
        }
      }
    },
    [previewError, previewOpen, previewSelection, previewUrl, secondScreen],
  );

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
      <div
        className={`navigator__layout${sidebarOpen ? "" : " navigator__layout--sidebar-closed"}`}
      >
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
              onPreviewFullscreen={handlePreviewFullscreen}
              onOpenSecondScreen={handleOpenSecondScreen}
              secondScreenActive={secondScreen.active}
              secondScreenStatus={secondScreen.status}
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
              paneRef={previewPaneRef}
            />
          </div>
        </main>
      </div>
    </div>
  );
}
