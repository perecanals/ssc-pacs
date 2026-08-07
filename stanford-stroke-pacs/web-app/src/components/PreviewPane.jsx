import PropTypes from "prop-types";
import usePaneResize from "../hooks/usePaneResize";
import usePaneFullscreen from "../hooks/usePaneFullscreen";
import "./PreviewPane.css";

export default function PreviewPane({
  selection,
  previewUrl,
  loading,
  loadingLabel,
  error,
  isOpen,
  height,
  onHeightChange,
  onCollapse,
  paneRef,
}) {
  const { resizing, handleProps } = usePaneResize({
    paneRef,
    onResize: onHeightChange,
  });
  const { isFullscreen, exit } = usePaneFullscreen({ paneRef });

  if (!selection) {
    return null;
  }

  // Deliberately NOT unmounted when collapsed: dropping the iframe would throw
  // away OHIF and every decoded frame, making re-open a full cold boot. Hiding
  // costs one study's frames in memory and makes re-open free.
  const classes = [
    "preview-pane",
    isOpen ? "preview-pane--open" : "preview-pane--collapsed",
    resizing ? "preview-pane--resizing" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <section
      ref={paneRef}
      className={classes}
      // Suppress the drag-resized height while fullscreen, so it can't fight
      // the :fullscreen rule. Restored on exit — the state is untouched.
      style={height != null && !isFullscreen ? { height } : undefined}
    >
      {!isFullscreen && (
        <div
          className="preview-pane__resize-handle"
          {...handleProps}
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize preview"
        />
      )}

      {(isFullscreen || onCollapse) && (
        // Sits over the top-left spot where OHIF's worklist-return control
        // (back arrow + logo) used to be — the injected CSS hides that control
        // because its route isn't served here (routes/proxy.py,
        // inject_viewer_close). Fullscreen exits back to the pane (Esc also
        // works — the browser handles it above the page, which matters because
        // keydown never reaches us when focus is inside the iframe); pane mode
        // collapses.
        <button
          type="button"
          onClick={isFullscreen ? exit : onCollapse}
          className="preview-pane__viewer-close"
        >
          <span aria-hidden="true">←</span>{" "}
          {isFullscreen ? (
            <>
              Exit fullscreen <span aria-hidden="true">(Esc)</span>
            </>
          ) : (
            "Collapse"
          )}
        </button>
      )}

      <div className="preview-pane__body">
        {loading && (
          <div className="preview-pane__state">
            {loadingLabel || "Resolving OHIF preview…"}
          </div>
        )}

        {!loading && error && (
          <div className="preview-pane__state preview-pane__state--error">
            {error}
          </div>
        )}

        {!loading && !error && previewUrl && (
          <iframe
            key={previewUrl}
            src={previewUrl}
            title="OHIF preview"
            className="preview-pane__frame"
          />
        )}
      </div>
    </section>
  );
}

PreviewPane.propTypes = {
  selection: PropTypes.object,
  previewUrl: PropTypes.string,
  loading: PropTypes.bool,
  loadingLabel: PropTypes.string,
  error: PropTypes.string,
  isOpen: PropTypes.bool,
  height: PropTypes.number,
  onHeightChange: PropTypes.func,
  onCollapse: PropTypes.func,
  paneRef: PropTypes.object,
};
