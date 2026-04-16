import PropTypes from "prop-types";
import "./PreviewPane.css";

function previewDescription(selection) {
  if (!selection) return "Click a study or series row to preview images in OHIF.";
  if (selection.sourceLevel === "series") {
    return selection.description
      ? `Opening study ${selection.studyinstanceuid} focused on series "${selection.description}".`
      : `Opening study ${selection.studyinstanceuid} focused on the clicked series.`;
  }
  return selection.description
    ? `Opening study "${selection.description}" in OHIF.`
    : `Opening study ${selection.studyinstanceuid} in OHIF.`;
}

export default function PreviewPane({
  selection,
  previewUrl,
  loading,
  loadingLabel,
  error,
  isOpen,
  onClose,
}) {
  if (!selection) {
    return null;
  }

  if (!isOpen) {
    return null;
  }

  return (
    <section className="preview-pane preview-pane--open">
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
          <>
            <iframe
              key={previewUrl}
              src={previewUrl}
              title="OHIF preview"
              className="preview-pane__frame"
            />
            <div className="preview-pane__overlay-actions">
              <a
                href={previewUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="preview-pane__overlay-btn"
              >
                Open in New Tab
              </a>
              <button type="button" onClick={onClose} className="preview-pane__overlay-btn">
                Collapse
              </button>
            </div>
          </>
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
  onClose: PropTypes.func,
};
