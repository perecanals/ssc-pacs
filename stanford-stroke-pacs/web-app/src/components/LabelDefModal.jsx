import { useEffect, useState } from "react";
import PropTypes from "prop-types";
import { apiGet, apiPatch, apiPost } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { valueColor } from "../utils/colors";
import "./LabelDefModal.css";

export default function LabelDefModal({
  defaultLevel = "series",
  existingLabel = null,
  onClose,
  onSaved,
}) {
  const isEdit = existingLabel != null;

  const [name, setName] = useState(existingLabel?.name || "");
  const [description, setDescription] = useState(
    existingLabel?.description || "",
  );
  const [level, setLevel] = useState(existingLabel?.level || defaultLevel);
  const [datatype, setDatatype] = useState(existingLabel?.datatype || "bool");
  const [options, setOptions] = useState(existingLabel?.options || []);
  const [optionInput, setOptionInput] = useState("");
  const [valueUsage, setValueUsage] = useState({});
  const [confirmRemove, setConfirmRemove] = useState(null); // {idx, opt, count}
  const [instrument, setInstrument] = useState(existingLabel?.instrument || "");
  const [instrumentSuggestions, setInstrumentSuggestions] = useState([]);
  const [error, setError] = useState("");

  // Who may edit this label's values. The self-service subset of the policy:
  // "everyone", "nobody", or just me ("users" with a single-entry list).
  // Arbitrary user lists are set by an admin under Label Access, which has the
  // user roster; picking one here would show it as "Selected users (n)".
  const [editPolicy, setEditPolicy] = useState(
    existingLabel?.edit_policy || "everyone",
  );
  const { isAdmin, currentUser } = useAuth();
  // Mirrors the server's can_change_label_policy: owner or admin.
  const mayChangePolicy =
    !isEdit || isAdmin || existingLabel?.created_by === currentUser;
  const existingUsers = existingLabel?.edit_users || [];
  // A list this modal cannot express (someone other than just me) — keep it
  // intact rather than silently rewriting it to [me] on an unrelated save.
  const listIsForeign =
    editPolicy === "users" &&
    !(existingUsers.length === 1 && existingUsers[0] === currentUser);

  // Mirrors the server's can_edit_label — the gate on writing values, which is
  // also the gate on editing the option vocabulary. Based on the *saved* policy
  // (the server checks the pre-PATCH row), so unlocking a "nobody" label and
  // editing its values takes two saves, same as inline edits. No admin bypass.
  const savedPolicy = existingLabel?.edit_policy || "everyone";
  const canEditValues =
    !isEdit ||
    savedPolicy === "everyone" ||
    (savedPolicy === "users" && existingUsers.includes(currentUser));

  useEffect(() => {
    apiGet("/api/instruments")
      .then((rows) =>
        setInstrumentSuggestions(rows.map((r) => r.name).filter(Boolean)),
      )
      .catch(() => setInstrumentSuggestions([]));
  }, []);

  // How many annotations hold each value — drives the remove confirmation.
  useEffect(() => {
    if (!isEdit || datatype !== "select") return;
    apiGet(`/api/labels/${encodeURIComponent(existingLabel.name)}/value-usage`)
      .then(setValueUsage)
      .catch(() => setValueUsage({}));
  }, [isEdit, datatype, existingLabel?.name]);

  const addOption = () => {
    const val = optionInput.trim();
    if (!val) return;
    if (options.some((o) => o.toLowerCase() === val.toLowerCase())) {
      setOptionInput("");
      return;
    }
    setOptions((prev) => [...prev, val]);
    setOptionInput("");
  };

  const removeOption = (idx) => {
    const opt = options[idx];
    const count = valueUsage[opt] || 0;
    if (isEdit && count > 0) {
      setConfirmRemove({ idx, opt, count });
      return;
    }
    setOptions((prev) => prev.filter((_, i) => i !== idx));
  };

  const confirmRemoveOption = () => {
    setOptions((prev) => prev.filter((_, i) => i !== confirmRemove.idx));
    setConfirmRemove(null);
  };

  // "Only me" is just a one-entry list — the server has no separate concept.
  // A foreign list (set under Label Access) is passed through untouched.
  const policyPayload = () => {
    if (editPolicy !== "users")
      return { edit_policy: editPolicy, edit_users: [] };
    return {
      edit_policy: "users",
      edit_users: listIsForeign ? existingUsers : [currentUser],
    };
  };

  const handleSave = async () => {
    setError("");

    if (isEdit) {
      const res = await apiPatch(`/api/label-definitions/${existingLabel.id}`, {
        description: description.trim() || null,
        instrument: instrument.trim() || null,
        ...(datatype === "select" && canEditValues ? { options } : {}),
        ...(mayChangePolicy ? policyPayload() : {}),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.detail || "Failed to update label definition");
        return;
      }
      onSaved();
      return;
    }

    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    // Mirrors the server's LABEL_NAME_RE — catch it here so the error points
    // at the Name field instead of surfacing as a generic save failure.
    if (!/^[A-Za-z][A-Za-z0-9_]{0,62}$/.test(name.trim())) {
      setError(
        "Name may only contain letters, digits and underscores, must start " +
          "with a letter, and be at most 63 characters (it becomes a column " +
          "in the exported tables). The values of a Select label are not " +
          "restricted.",
      );
      return;
    }
    const res = await apiPost("/api/label-definitions", {
      name: name.trim(),
      description: description.trim() || null,
      level,
      datatype,
      options: datatype === "select" && options.length > 0 ? options : null,
      instrument: instrument.trim() || null,
      ...policyPayload(),
    });
    if (res.status === 409) {
      setError("A label with this name already exists");
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      setError(body?.detail || "Failed to create label definition");
      return;
    }
    onSaved();
  };

  return (
    <div
      className="label-modal__overlay"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="label-modal">
        <h3 className="label-modal__title">
          {isEdit
            ? `Edit label: ${existingLabel.name}`
            : "Define New Label Type"}
        </h3>

        <label className="label-modal__label">Name *</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. hemorrhagic, infarct_volume"
          className="label-modal__input"
          autoFocus={!isEdit}
          disabled={isEdit}
        />

        <label className="label-modal__label">Description</label>
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this label means..."
          className="label-modal__input"
        />

        <label className="label-modal__label">Instrument</label>
        <input
          type="text"
          value={instrument}
          onChange={(e) => setInstrument(e.target.value)}
          placeholder="e.g. Functional outcome, Imaging quality"
          className="label-modal__input"
          list="label-modal-instruments"
          autoFocus={isEdit}
        />
        <datalist id="label-modal-instruments">
          {instrumentSuggestions.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>

        <label className="label-modal__label">Level</label>
        <select
          value={level}
          onChange={(e) => setLevel(e.target.value)}
          className="label-modal__select"
          disabled={isEdit}
        >
          <option value="patient">Patient</option>
          <option value="study">Study</option>
          <option value="series">Series</option>
        </select>

        <label className="label-modal__label">Data Type</label>
        <select
          value={datatype}
          onChange={(e) => setDatatype(e.target.value)}
          className="label-modal__select"
          disabled={isEdit}
        >
          <option value="bool">Boolean (present / absent)</option>
          <option value="int">Integer (numeric value)</option>
          <option value="text">Text (free-form value)</option>
          <option value="select">Select (pick from predefined values)</option>
        </select>

        <label className="label-modal__label">Who can edit values</label>
        <select
          value={editPolicy}
          onChange={(e) => setEditPolicy(e.target.value)}
          className="label-modal__select"
          disabled={!mayChangePolicy}
        >
          <option value="everyone">Everyone</option>
          <option value="users">
            {listIsForeign
              ? `Selected users (${existingUsers.length})`
              : "Only me"}
          </option>
          <option value="nobody">No one</option>
        </select>
        <p className="label-modal__hint">
          {!mayChangePolicy
            ? "Only the label's owner or an admin can change this."
            : editPolicy === "nobody"
              ? "No one can edit these values — not even an admin. Change this setting first to correct a value."
              : listIsForeign
                ? `Editable by: ${existingUsers.join(", ")}. Change the list under Label Access.`
                : "Admins can grant specific users under Label Access."}
        </p>

        {datatype === "select" && (
          <div className="label-modal__options-section">
            <label className="label-modal__label">
              {isEdit ? "Values" : "Initial Values"}
            </label>
            <p className="label-modal__options-hint">
              {canEditValues
                ? "Add or remove values users can pick from."
                : "You don't have permission to edit this label's values."}
            </p>
            {canEditValues && (
              <div className="label-modal__options-row">
                <input
                  type="text"
                  value={optionInput}
                  onChange={(e) => setOptionInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addOption();
                    }
                  }}
                  placeholder="Type a value and press Enter"
                  className="label-modal__option-input"
                />
                <button
                  type="button"
                  onClick={addOption}
                  className="btn-outline"
                >
                  Add
                </button>
              </div>
            )}
            {options.length > 0 && (
              <div className="label-modal__pills">
                {options.map((opt, i) => {
                  const c = valueColor(opt);
                  return (
                    <span
                      key={i}
                      style={{ backgroundColor: c.bg, color: c.text }}
                      className="label-modal__pill"
                    >
                      {opt}
                      {canEditValues && (
                        <button
                          type="button"
                          onClick={() => removeOption(i)}
                          className="label-modal__pill-remove"
                        >
                          &times;
                        </button>
                      )}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {confirmRemove && (
          <div
            className="label-modal__confirm-overlay"
            onClick={(e) =>
              e.target === e.currentTarget && setConfirmRemove(null)
            }
          >
            <div className="label-modal__confirm">
              <p className="label-modal__confirm-text">
                <strong>{confirmRemove.opt}</strong> is currently assigned to{" "}
                <strong>{confirmRemove.count}</strong>{" "}
                {confirmRemove.count === 1 ? "entry" : "entries"}. Removing it
                only takes it off the pick list — existing entries keep the
                value.
              </p>
              <div className="label-modal__actions">
                <button
                  type="button"
                  onClick={() => setConfirmRemove(null)}
                  className="btn-outline"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={confirmRemoveOption}
                  className="btn-primary"
                >
                  Remove
                </button>
              </div>
            </div>
          </div>
        )}

        {error && <p className="label-modal__error">{error}</p>}

        <div className="label-modal__actions">
          <button onClick={onClose} className="btn-outline">
            Cancel
          </button>
          <button onClick={handleSave} className="btn-primary">
            {isEdit ? "Save" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

LabelDefModal.propTypes = {
  defaultLevel: PropTypes.oneOf(["patient", "study", "series"]),
  existingLabel: PropTypes.shape({
    id: PropTypes.number.isRequired,
    name: PropTypes.string.isRequired,
    description: PropTypes.string,
    level: PropTypes.string,
    datatype: PropTypes.string,
    options: PropTypes.array,
    instrument: PropTypes.string,
    created_by: PropTypes.string,
    edit_policy: PropTypes.string,
    edit_users: PropTypes.arrayOf(PropTypes.string),
  }),
  onClose: PropTypes.func.isRequired,
  onSaved: PropTypes.func.isRequired,
};
