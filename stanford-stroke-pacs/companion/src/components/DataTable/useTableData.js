import { useState, useCallback, useEffect } from "react";
import { apiGet } from "../../api/client";
import {
  PER_PAGE,
  normalizeSelectFilterValues,
  hasFilterValue,
  getTextFilterValue,
} from "../../utils/table";

export default function useTableData({ level, config, filters, page, sortBy, sortDir, columnFilters, allCols }) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);

  const fetchItems = useCallback(async () => {
    const params = new URLSearchParams({
      page: String(page),
      per_page: String(PER_PAGE),
      sort_by: sortBy,
      sort_dir: sortDir,
    });

    if (filters.label) {
      params.set("label", filters.label);
      if (filters.labelLevel) params.set("label_level", filters.labelLevel);
    }
    if (filters.patientId) params.set("patient_id", filters.patientId);
    if (filters.modality) params.set("modality", filters.modality);
    if (filters.description) params.set("description", filters.description);
    if (level === "patient" && filters.studyImportLabel?.trim()) {
      params.set("study_import_label", filters.studyImportLabel.trim());
    }

    const labelFilters = [];
    for (const [key, val] of Object.entries(columnFilters)) {
      if (!hasFilterValue(val)) continue;
      if (key.startsWith("label:")) {
        const col = allCols.find((c) => c.key === key);
        const datatype = col?.datatype || "text";
        if (datatype === "select") {
          const values = normalizeSelectFilterValues(val);
          if (values.length === 0) continue;
          labelFilters.push({
            label: key.replace("label:", ""),
            level: col?.level || level,
            values,
            datatype,
          });
        } else {
          labelFilters.push({
            label: key.replace("label:", ""),
            level: col?.level || level,
            value: getTextFilterValue(val),
            datatype,
          });
        }
      } else {
        const param = config.filterParamMap[key];
        if (param && typeof val === "string") params.set(param, val);
      }
    }
    if (labelFilters.length > 0) {
      params.set("label_filters", JSON.stringify(labelFilters));
    }

    try {
      const data = await apiGet(`${config.endpoint}?${params}`);
      setItems(data[config.itemsKey]);
      setTotal(data.total);
    } catch {
      setItems([]);
      setTotal(0);
    }
  }, [page, filters, sortBy, sortDir, columnFilters, config, allCols, level]);

  useEffect(() => { fetchItems(); }, [fetchItems]);

  return { items, total, fetchItems };
}
