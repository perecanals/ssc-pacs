import { useState, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import PropTypes from "prop-types";
import { useAuth } from "../context/AuthContext";
import { apiGet, apiPost, apiDelete } from "../api/client";
import { valueColor } from "../utils/colors";
import { compareSelectValues } from "../utils/table";
import "./InlineEdit.css";

function SelectPill({ value, onClick, className = "" }) {
  const c = valueColor(value);
  return (
    <span
      onClick={onClick}
      style={{ backgroundColor: c.bg, color: c.text }}
      className={`select-pill${onClick ? " select-pill--clickable" : ""} ${className}`}
    >
      {value}
    </span>
  );
}

function buildPayload(level, entity, labelName, value) {
  const base = { level, label: labelName, value };
  if (level === "patient") {
    return { ...base, patient_id: entity.patient_id };
  }
  if (level === "study") {
    return { ...base, studyinstanceuid: entity.studyinstanceuid, patient_id: entity.patient_id };
  }
  return {
    ...base,
    seriesinstanceuid: entity.seriesinstanceuid,
    studyinstanceuid: entity.studyinstanceuid,
    patient_id: entity.patient_id,
  };
}

export default function InlineEdit({
  level = "series",
  entity,
  labelName,
  datatype,
  defOptions = [],
  annotations,
  onMutated,
}) {

  const { currentUser } = useAuth();
  const ann = annotations.find((a) => a.label === labelName) || null;

  if (!currentUser) {
    if (datatype === "bool") {
      return ann ? <span className="inline-edit__check">&#10003;</span> : null;
    }
    if (datatype === "select") {
      return ann?.value ? <SelectPill value={ann.value} /> : null;
    }
    return <span>{ann?.value || ""}</span>;
  }

  if (datatype === "bool") {
    return (
      <BoolEdit
        level={level}
        entity={entity}
        labelName={labelName}
        ann={ann}
        onMutated={onMutated}
      />
    );
  }

  if (datatype === "select") {
    return (
      <SelectEdit
        level={level}
        entity={entity}
        labelName={labelName}
        defOptions={defOptions}
        ann={ann}
        onMutated={onMutated}
      />
    );
  }

  return (
    <ValueEdit
      level={level}
      entity={entity}
      labelName={labelName}
      datatype={datatype}
      ann={ann}
      onMutated={onMutated}
    />
  );
}

function BoolEdit({ level, entity, labelName, ann, onMutated }) {
  const [saving, setSaving] = useState(false);
  // Optimistic override: undefined = trust the `ann` prop; true/false = show this
  // value immediately while the request is in flight.
  const [pending, setPending] = useState(undefined);

  const annChecked = !!ann;

  // Drop the override once the reloaded prop catches up to what we optimistically
  // showed, so the cell switches to prop-sourced rendering without a flicker.
  useEffect(() => {
    if (pending !== undefined && pending === annChecked) setPending(undefined);
  }, [annChecked, pending]);

  const handleChange = async (e) => {
    const next = e.target.checked;
    setPending(next);
    setSaving(true);
    try {
      const res = next
        ? await apiPost("/api/annotations", buildPayload(level, entity, labelName, null))
        : ann
          ? await apiDelete(`/api/annotations/${ann.id}`)
          : { ok: true };
      if (!res.ok) {
        setPending(undefined);
        alert("Could not save annotation");
        return;
      }
      onMutated();
    } finally {
      setSaving(false);
    }
  };

  return (
    <span title={ann?.created_by ? `Last edited by ${ann.created_by}` : undefined}>
      <input
        type="checkbox"
        checked={pending ?? annChecked}
        onChange={handleChange}
        disabled={saving}
        className="bool-edit__checkbox"
      />
    </span>
  );
}

// Keep in sync with .select-edit__dropdown in InlineEdit.css (max-h / min-w).
const DROPDOWN_MAX_H = 260;
const DROPDOWN_MIN_W = 200;
const DROPDOWN_MARGIN = 8;

function SelectEdit({ level, entity, labelName, defOptions = [], ann, onMutated }) {
  const [open, setOpen] = useState(false);
  // Fixed-position coordinates for the portaled dropdown, computed from the
  // trigger's rect at open time. {top} opens downward, {bottom} upward.
  const [pos, setPos] = useState(null);
  const [annValues, setAnnValues] = useState([]);
  const [search, setSearch] = useState("");
  const [saving, setSaving] = useState(false);
  // Optimistic override: undefined = trust the `ann` prop; otherwise the selected
  // string, or null when the value was cleared.
  const [pending, setPending] = useState(undefined);
  const ref = useRef(null);
  const dropdownRef = useRef(null);

  const annValue = ann?.value ?? null;

  // Drop the override once the reloaded prop catches up, without a flicker.
  useEffect(() => {
    if (pending !== undefined && pending === annValue) setPending(undefined);
  }, [annValue, pending]);

  // The dropdown is portaled to <body> with position:fixed so it can't be
  // clipped by the sub-tables' overflow-auto scroll wrappers (.dt__child-scroll
  // / .dt__gc-scroll). Anchor it to the trigger's rect, flipping upward when
  // there isn't room below, and clamping horizontally to the viewport.
  const openDropdown = () => {
    const rect = ref.current.getBoundingClientRect();
    const spaceBelow = window.innerHeight - rect.bottom;
    const openUp = spaceBelow < DROPDOWN_MAX_H + DROPDOWN_MARGIN && rect.top > spaceBelow;
    setPos({
      left: Math.max(
        DROPDOWN_MARGIN,
        Math.min(rect.left, window.innerWidth - DROPDOWN_MIN_W - DROPDOWN_MARGIN),
      ),
      top: openUp ? undefined : rect.bottom + 4,
      bottom: openUp ? window.innerHeight - rect.top + 4 : undefined,
    });
    setOpen(true);
  };

  useEffect(() => {
    const close = (e) => {
      if (ref.current?.contains(e.target)) return;
      if (dropdownRef.current?.contains(e.target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  // A fixed-position dropdown doesn't follow its anchor, so close it when any
  // ancestor scrolls (capture phase catches the table's scroll containers) or
  // the window resizes. Scrolling the option list itself stays open.
  useEffect(() => {
    if (!open) return undefined;
    const onScroll = (e) => {
      if (dropdownRef.current?.contains(e.target)) return;
      setOpen(false);
    };
    const onResize = () => setOpen(false);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);

  useEffect(() => {
    if (open) {
      apiGet(`/api/labels/${encodeURIComponent(labelName)}/values`)
        .then(setAnnValues)
        .catch(() => setAnnValues([]));
    }
  }, [open, labelName]);

  const allOptions = [...new Set([...defOptions, ...annValues])].sort(compareSelectValues);
  const filtered = allOptions.filter((v) =>
    v.toLowerCase().includes(search.toLowerCase()),
  );
  const trimmed = search.trim();
  const showCreate = trimmed && !allOptions.some((v) => v.toLowerCase() === trimmed.toLowerCase());

  const handleSelect = async (value) => {
    const isClear = value === ann?.value;
    // Reflect the choice immediately and close the dropdown so the new pill shows.
    setPending(isClear ? null : value);
    setOpen(false);
    setSearch("");
    setSaving(true);
    try {
      const res = isClear
        ? await apiDelete(`/api/annotations/${ann.id}`)
        : await apiPost("/api/annotations", buildPayload(level, entity, labelName, value));
      if (!res.ok) {
        setPending(undefined);
        alert("Could not save annotation");
        return;
      }
      onMutated();
    } finally {
      setSaving(false);
    }
  };

  const currentValue = pending !== undefined ? pending : ann?.value;

  const toggleOpen = () => (open ? setOpen(false) : openDropdown());

  return (
    <span className="select-edit" ref={ref} title={ann?.created_by ? `Last edited by ${ann.created_by}` : undefined}>
      {currentValue ? (
        <SelectPill value={currentValue} onClick={toggleOpen} />
      ) : (
        <button
          onClick={toggleOpen}
          className="select-edit__placeholder"
        >
          Select&hellip;
        </button>
      )}
      {open && pos && createPortal(
        <div
          className="select-edit__dropdown"
          ref={dropdownRef}
          style={{ top: pos.top, bottom: pos.bottom, left: pos.left }}
        >
          <div className="select-edit__search-wrap">
            <input
              type="text"
              placeholder="Search or create…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && showCreate) handleSelect(trimmed);
              }}
              className="select-edit__search-input"
              autoFocus
            />
          </div>
          <div className="select-edit__options">
            {currentValue && (
              <button
                onClick={() => handleSelect(currentValue)}
                disabled={saving}
                className="select-edit__clear-btn"
              >
                Clear value
              </button>
            )}
            {filtered.map((v) => (
              <button
                key={v}
                onClick={() => handleSelect(v)}
                disabled={saving}
                className="select-edit__option-btn"
              >
                <SelectPill value={v} />
                {v === currentValue && (
                  <span className="select-edit__check-mark">&#10003;</span>
                )}
              </button>
            ))}
            {showCreate && (
              <button
                onClick={() => handleSelect(trimmed)}
                disabled={saving}
                className="select-edit__create-btn"
              >
                Create &ldquo;<span className="font-semibold">{trimmed}</span>&rdquo;
              </button>
            )}
            {filtered.length === 0 && !showCreate && (
              <div className="select-edit__empty">
                No options yet
              </div>
            )}
          </div>
        </div>,
        document.body,
      )}
    </span>
  );
}

function ValueEdit({
  level,
  entity,
  labelName,
  datatype,
  ann,
  onMutated,
}) {
  const [value, setValue] = useState(ann?.value || "");
  const originalRef = useRef(ann?.value || "");
  const [saving, setSaving] = useState(false);

  const doSave = async () => {
    const trimmed = value.trim();
    if (trimmed === originalRef.current) return;

    setSaving(true);
    try {
      if (trimmed) {
        if (datatype === "int" && isNaN(Number(trimmed))) {
          setValue(originalRef.current);
          return;
        }
        const res = await apiPost("/api/annotations", buildPayload(level, entity, labelName, trimmed));
        if (!res.ok) {
          setValue(originalRef.current);
          alert("Could not save annotation");
          return;
        }
      } else if (ann) {
        const res = await apiDelete(`/api/annotations/${ann.id}`);
        if (!res.ok) {
          setValue(originalRef.current);
          alert("Could not save annotation");
          return;
        }
      }
      // Commit the new baseline only on success, so a failed save can't bake in
      // an unsaved value.
      originalRef.current = trimmed;
      onMutated();
    } finally {
      setSaving(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter") e.target.blur();
    if (e.key === "Escape") {
      setValue(originalRef.current);
      e.target.blur();
    }
  };

  return (
    <span title={ann?.created_by ? `Last edited by ${ann.created_by}` : undefined}>
      <input
        type={datatype === "int" ? "number" : "text"}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={doSave}
        onKeyDown={handleKeyDown}
        disabled={saving}
        className={`value-edit__input ${saving ? "value-edit__input--saving" : ""} ${
          datatype === "int" ? "value-edit__input--int" : "value-edit__input--text"
        }`}
      />
    </span>
  );
}

InlineEdit.propTypes = {
  level: PropTypes.oneOf(["patient", "study", "series"]),
  entity: PropTypes.object.isRequired,
  labelName: PropTypes.string.isRequired,
  datatype: PropTypes.oneOf(["bool", "int", "text", "select"]).isRequired,
  defOptions: PropTypes.arrayOf(PropTypes.string),
  annotations: PropTypes.array.isRequired,
  onMutated: PropTypes.func.isRequired,
};
