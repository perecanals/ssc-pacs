import { useEffect, useState } from "react";
import PropTypes from "prop-types";
import { fetchDeletionPlan, deleteEntity } from "./DataTable/actions";
import "./DeleteEntityModal.css";

/**
 * Admin-only confirmation modal for deleting a study or series.
 *
 * Loads the deletion plan (series count, annotations that will be discarded) and
 * requires an explicit confirm. The delete is complete and permanent: it removes
 * the entity from Orthanc, the database, and disk (DICOM files + archives).
 */
export default function DeleteEntityModal({
  level,
  uid,
  label,
  onClose,
  onDeleted,
}) {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchDeletionPlan(level, uid)
      .then((p) => {
        if (alive) setPlan(p);
      })
      .catch((e) => {
        if (alive) setError(e.message || "Failed to load deletion plan");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [level, uid]);

  const handleDelete = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await deleteEntity(level, uid);
      onDeleted(res);
    } catch (e) {
      setError(e.message || "Delete failed");
      setBusy(false);
    }
  };

  return (
    <div className="del-modal__overlay" onClick={onClose}>
      <div className="del-modal" onClick={(e) => e.stopPropagation()}>
        <div className="del-modal__title">
          Delete {level} —{" "}
          <span className="del-modal__uid">{label || uid}</span>
        </div>

        {loading && <div className="del-modal__body">Loading plan…</div>}

        {!loading && plan && (
          <div className="del-modal__body">
            <ul className="del-modal__list">
              <li>
                Patient <strong>{plan.patient_id}</strong>
                {level === "study" && (
                  <>
                    {" · "}
                    <strong>{plan.n_series}</strong> series
                  </>
                )}
              </li>
              <li>
                Permanently removes it from <strong>Orthanc</strong> (viewer
                index), the <strong>database</strong>, and <strong>disk</strong>{" "}
                (DICOM files + archives).
              </li>
              <li className="del-modal__warn">
                Discards <strong>{plan.n_annotations_discarded}</strong>{" "}
                annotation
                {plan.n_annotations_discarded === 1 ? "" : "s"} (kept in
                history, not migrated).
              </li>
              <li className="del-modal__note">
                This cannot be undone from the UI.
              </li>
            </ul>
          </div>
        )}

        {error && <div className="del-modal__error">{error}</div>}

        <div className="del-modal__actions">
          <button className="del-modal__btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="del-modal__btn del-modal__btn--danger"
            onClick={handleDelete}
            disabled={busy || loading || !plan}
          >
            {busy ? "Deleting…" : `Delete ${level}`}
          </button>
        </div>
      </div>
    </div>
  );
}

DeleteEntityModal.propTypes = {
  level: PropTypes.oneOf(["study", "series"]).isRequired,
  uid: PropTypes.string.isRequired,
  label: PropTypes.string,
  onClose: PropTypes.func.isRequired,
  onDeleted: PropTypes.func.isRequired,
};
