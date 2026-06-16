const LEVEL_RANK = { patient: 0, study: 1, series: 2 };
const LEVEL_ORDER = ["patient", "study", "series"];
const LEVEL_LABELS = { patient: "Patient", study: "Study", series: "Series" };

const LEVEL_CONFIG = {
  patient: {
    endpoint: "/api/patients",
    itemsKey: "items",
    idCol: "patient_id",
    entityLabel: "patients",
    builtinCols: [
      { key: "patient_id", label: "Patient ID", filterable: true },
      { key: "stroke_date", label: "Stroke Date", filterable: true },
      { key: "study_import_labels", label: "Study import labels", filterable: true, sortable: false, defaultVisible: false },
      { key: "dataset", label: "Dataset", filterable: true, sortable: false },
    ],
    sortDefault: "patient_id",
    filterParamMap: { patient_id: "patient_id", stroke_date: "stroke_date", study_import_labels: "study_import_label", dataset: "dataset" },
    expandable: true,
    expandEndpoint: (row) => `/api/patients/${encodeURIComponent(row.patient_id)}/studies`,
    childLevel: "study",
  },
  study: {
    endpoint: "/api/studies",
    itemsKey: "items",
    idCol: "studyinstanceuid",
    entityLabel: "studies",
    builtinCols: [
      { key: "patient_id", label: "Patient ID", filterable: true },
      { key: "dataset", label: "Dataset", filterable: true, sortable: false },
      { key: "import_id", label: "Import ID", filterable: true, defaultVisible: false },
      { key: "import_label", label: "Import Label", filterable: true, defaultVisible: false },
      { key: "acquisitiondatetime", label: "Acquisition Date", filterable: true },
      { key: "modality", label: "Modality", filterable: true },
      { key: "studydescription", label: "Study Description", filterable: true },
    ],
    sortDefault: "patient_id",
    filterParamMap: {
      patient_id: "patient_id",
      dataset: "dataset",
      import_id: "import_id",
      import_label: "import_label",
      acquisitiondatetime: "acquisitiondatetime",
      modality: "modality",
      studydescription: "studydescription",
    },
    expandable: true,
    expandEndpoint: (row) => `/api/studies/${encodeURIComponent(row.studyinstanceuid)}/series`,
    childLevel: "series",
  },
  series: {
    endpoint: "/api/series",
    itemsKey: "series",
    idCol: "seriesinstanceuid",
    entityLabel: "series",
    builtinCols: [
      { key: "patient_id", label: "Patient ID", filterable: true },
      { key: "dataset", label: "Dataset", filterable: true, sortable: false },
      { key: "import_id", label: "Import ID", filterable: true, defaultVisible: false },
      { key: "import_label", label: "Import Label", filterable: true, defaultVisible: false },
      { key: "acquisitiondatetime", label: "Acquisition Date", filterable: true },
      { key: "modality", label: "Modality", filterable: true },
      { key: "seriesdescription", label: "Series Description", filterable: true },
      { key: "number_of_slices", label: "Slices", filterable: false },
      { key: "slicethickness", label: "Slice Thickness (mm)", filterable: true },
      { key: "scanaxialcoverage_mm", label: "Axial Coverage (mm)", filterable: true },
    ],
    sortDefault: "patient_id",
    filterParamMap: {
      patient_id: "patient_id",
      dataset: "dataset",
      import_id: "import_id",
      import_label: "import_label",
      acquisitiondatetime: "acquisitiondatetime",
      modality: "modality",
      seriesdescription: "description",
      slicethickness: "slicethickness",
      scanaxialcoverage_mm: "scanaxialcoverage",
    },
    expandable: false,
  },
};

export { LEVEL_RANK, LEVEL_ORDER, LEVEL_LABELS, LEVEL_CONFIG };

export const PER_PAGE = 50;

// Annotation labels shown as columns by default (when the user has no saved
// column preferences). Matched by label name; a label only defaults on at
// table levels where it applies (its level is at or below the active level,
// same rule as built-in child-level columns).
export const DEFAULT_VISIBLE_LABEL_NAMES = ["timepoint", "series_type"];

// Shared default ordering for annotation labels (used by both the default
// column order in the data table and the sidebar quick-filter list, so the
// two stay consistent): grouped by instrument (alphabetical, unassigned/null
// last), then by label creation time (oldest first) within each instrument.
// Name is a stable tiebreak when timestamps are equal/missing. Accepts both
// label-definition objects (`.name`) and labels-summary rows (`.label`).
export function compareLabelDefsDefault(a, b) {
  const ai = a.instrument || null;
  const bi = b.instrument || null;
  if (ai !== bi) {
    if (ai === null) return 1;
    if (bi === null) return -1;
    return ai.localeCompare(bi);
  }
  const at = Number.isNaN(Date.parse(a.created_at)) ? 0 : Date.parse(a.created_at);
  const bt = Number.isNaN(Date.parse(b.created_at)) ? 0 : Date.parse(b.created_at);
  if (at !== bt) return at - bt;
  return (a.name || a.label || "").localeCompare(b.name || b.label || "");
}

export function buildBuiltinColumnCatalog(activeLevel) {
  return LEVEL_ORDER.flatMap((builtinLevel) =>
    LEVEL_CONFIG[builtinLevel].builtinCols.map((col) => ({
      ...col,
      key: `builtin:${builtinLevel}:${col.key}`,
      sourceKey: col.key,
      level: builtinLevel,
      defaultVisible:
        LEVEL_RANK[builtinLevel] >= LEVEL_RANK[activeLevel] && col.defaultVisible !== false,
      legacyKeys:
        builtinLevel === activeLevel
          ? [col.key, ...(col.key === "import_id" ? ["integration_id"] : [])]
          : [],
    })),
  );
}

export function formatDatetime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-CA") + " " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// Round a numeric value to at most 2 decimals without forcing trailing zeros
// (1.3, not 1.30). Blank for null/empty; passes non-numeric values through.
export function formatNumber(v) {
  if (v === "" || v == null) return "";
  const n = Number(v);
  return Number.isNaN(n) ? v : Math.round(n * 100) / 100;
}

export function buildPatientStudiesUrl(row, studyImportLabel) {
  const base = `/api/patients/${encodeURIComponent(row.patient_id)}/studies`;
  const v = typeof studyImportLabel === "string" ? studyImportLabel.trim() : "";
  if (!v) return base;
  return `${base}?study_import_label=${encodeURIComponent(v)}`;
}

export function normalizeSelectFilterValues(value) {
  if (Array.isArray(value)) {
    return value
      .map((item) => (typeof item === "string" ? item.trim() : ""))
      .filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) {
    return [value.trim()];
  }
  return [];
}

export function hasFilterValue(value) {
  if (Array.isArray(value)) {
    return normalizeSelectFilterValues(value).length > 0;
  }
  return value != null && value !== "";
}

export function getTextFilterValue(value) {
  return typeof value === "string" ? value : "";
}
