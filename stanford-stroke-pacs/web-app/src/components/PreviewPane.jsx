import { useRef } from "react";
import PropTypes from "prop-types";
import usePaneResize from "../hooks/usePaneResize";
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
}) {
  const paneRef = useRef(null);
  const { resizing, handleProps } = usePaneResize({ paneRef, onResize: onHeightChange });

  if (!selection) {
    return null;
  }

  if (!isOpen) {
    return null;
  }

  return (
    <section
      ref={paneRef}
      className={`preview-pane preview-pane--open${resizing ? " preview-pane--resizing" : ""}`}
      style={height != null ? { height } : undefined}
    >
      <div
        className="preview-pane__resize-handle"
        {...handleProps}
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize preview"
      />
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
};
