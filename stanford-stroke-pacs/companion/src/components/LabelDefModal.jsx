import { useState } from "react";
import { apiPost } from "../api/client";
import "./LabelDefModal.css";

const NOTION_COLORS = [
  { bg: "#f3e8ff", text: "#7c3aed" },
  { bg: "#dbeafe", text: "#2563eb" },
  { bg: "#dcfce7", text: "#16a34a" },
  { bg: "#fef3c7", text: "#d97706" },
  { bg: "#ffe4e6", text: "#e11d48" },
  { bg: "#ffedd5", text: "#ea580c" },
  { bg: "#e0f2fe", text: "#0284c7" },
  { bg: "#e0e7ff", text: "#4f46e5" },
  { bg: "#fce7f3", text: "#be185d" },
  { bg: "#ccfbf1", text: "#0d9488" },
];

function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function valueColor(value) {
  return NOTION_COLORS[hashStr(value) % NOTION_COLORS.length];
}

export default function LabelDefModal({ defaultLevel = "series", onClose, onCreated }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [level, setLevel] = useState(defaultLevel);
  const [datatype, setDatatype] = useState("bool");
  const [options, setOptions] = useState([]);
  const [optionInput, setOptionInput] = useState("");
  const [error, setError] = useState("");

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
    setOptions((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleSave = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    const res = await apiPost("/api/label-definitions", {
      name: name.trim(),
      description: description.trim() || null,
      level,
      datatype,
      options: datatype === "select" && options.length > 0 ? options : null,
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
    onCreated();
  };

  return (
    <div
      className="label-modal__overlay"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="label-modal">
        <h3 className="label-modal__title">
          Define New Label Type
        </h3>

        <label className="label-modal__label">
          Name *
        </label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. hemorrhagic, infarct_volume"
          className="label-modal__input"
          autoFocus
        />

        <label className="label-modal__label">
          Description
        </label>
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this label means..."
          className="label-modal__input"
        />

        <label className="label-modal__label">
          Level
        </label>
        <select
          value={level}
          onChange={(e) => setLevel(e.target.value)}
          className="label-modal__select"
        >
          <option value="patient">Patient</option>
          <option value="study">Study</option>
          <option value="series">Series</option>
        </select>

        <label className="label-modal__label">
          Data Type
        </label>
        <select
          value={datatype}
          onChange={(e) => setDatatype(e.target.value)}
          className="label-modal__select"
        >
          <option value="bool">Boolean (present / absent)</option>
          <option value="int">Integer (numeric value)</option>
          <option value="text">Text (free-form value)</option>
          <option value="select">Select (pick from predefined values)</option>
        </select>

        {datatype === "select" && (
          <div className="label-modal__options-section">
            <label className="label-modal__label">
              Initial Values
            </label>
            <p className="label-modal__options-hint">
              Add values users can pick from. More can be added later.
            </p>
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
                      <button
                        type="button"
                        onClick={() => removeOption(i)}
                        className="label-modal__pill-remove"
                      >
                        &times;
                      </button>
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {error && (
          <p className="label-modal__error">{error}</p>
        )}

        <div className="label-modal__actions">
          <button onClick={onClose} className="btn-outline">
            Cancel
          </button>
          <button onClick={handleSave} className="btn-primary">
            Create
          </button>
        </div>
      </div>
    </div>
  );
}
