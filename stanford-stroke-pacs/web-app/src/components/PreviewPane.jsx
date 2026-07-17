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

      {isFullscreen && (
        // The DataTable footer (and its Exit affordance) is not visible while
        // fullscreen, so the pane carries its own. Esc also works — the browser
        // handles it above the page, which matters because keydown never
        // reaches us when focus is inside the cross-origin iframe.
        <button
          type="button"
          onClick={exit}
          className="preview-pane__exit-fullscreen"
        >
          Exit fullscreen <span aria-hidden="true">(Esc)</span>
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
  paneRef: PropTypes.object,
};
