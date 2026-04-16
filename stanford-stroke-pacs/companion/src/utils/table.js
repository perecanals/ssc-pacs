const LEVEL_RANK = { patient: 0, study: 1, series: 2 };
const LEVEL_ORDER = ["patient", "study", "series"];

const LEVEL_CONFIG = {
  patient: {
    endpoint: "/api/patients",
    itemsKey: "items",
    idCol: "patient_id",
    entityLabel: "patients",
    builtinCols: [
      { key: "patient_id", label: "Patient ID", filterable: true },
      { key: "stroke_date", label: "Stroke Date", filterable: true },
      { key: "study_import_labels", label: "Study import labels", filterable: true, sortable: false },
    ],
    sortDefault: "patient_id",
    filterParamMap: { patient_id: "patient_id", stroke_date: "stroke_date", study_import_labels: "study_import_label" },
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
      { key: "import_id", label: "Import ID", filterable: true, defaultVisible: false },
      { key: "import_label", label: "Import Label", filterable: true },
      { key: "acquisitiondatetime", label: "Acquisition Date", filterable: true },
      { key: "modality", label: "Modality", filterable: true },
      { key: "studydescription", label: "Study Description", filterable: true },
    ],
    sortDefault: "patient_id",
    filterParamMap: {
      patient_id: "patient_id",
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
      { key: "import_id", label: "Import ID", filterable: true, defaultVisible: false },
      { key: "import_label", label: "Import Label", filterable: true },
      { key: "acquisitiondatetime", label: "Acquisition Date", filterable: true },
      { key: "modality", label: "Modality", filterable: true },
      { key: "seriesdescription", label: "Series Description", filterable: true },
      { key: "number_of_slices", label: "Slices", filterable: false },
    ],
    sortDefault: "patient_id",
    filterParamMap: {
      patient_id: "patient_id",
      import_id: "import_id",
      import_label: "import_label",
      acquisitiondatetime: "acquisitiondatetime",
      modality: "modality",
      seriesdescription: "description",
    },
    expandable: false,
  },
};

export { LEVEL_RANK, LEVEL_ORDER, LEVEL_CONFIG };

export const PER_PAGE = 50;

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
