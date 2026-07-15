import { useState, useCallback, useEffect, useRef } from "react";
import { apiGet } from "../../api/client";
import {
  PER_PAGE,
  normalizeSelectFilterValues,
  hasFilterValue,
  getTextFilterValue,
} from "../../utils/table";

// Infinite-scroll data hook. Rows accumulate across offset pages as the caller
// invokes loadMore(). A "reset" (any filter/sort/level/columnFilter change)
// replaces the accumulated list with page 1 and bumps resetNonce so the table
// can scroll back to the top. handleMutated uses reload() to re-fetch every
// currently loaded page (1..pageRef) in place — the deterministic backend
// ORDER BY tiebreaker guarantees the re-fetched window is identical, and
// expanded/child state (keyed by id, not by items index) survives the replace.
export default function useTableData({
  level,
  config,
  filters,
  sortBy,
  sortDir,
  columnFilters,
  allCols,
}) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [resetNonce, setResetNonce] = useState(0);

  // Mutable mirrors so loadMore/reset/reload stay referentially stable and can
  // self-guard without depending on render state.
  const pageRef = useRef(1);
  const seqRef = useRef(0);
  const loadingRef = useRef(false);
  const itemsLenRef = useRef(0);
  const totalRef = useRef(0);

  const buildParams = useCallback(
    (page) => {
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
      // Dataset + import-label sidebar quick filters apply at every level. The
      // import-label param name differs: patients use `study_import_label`
      // (matches across studies+series); studies/series use `import_label`.
      if (filters.dataset?.trim()) {
        params.set("dataset", filters.dataset.trim());
      }
      if (filters.studyImportLabel?.trim()) {
        const v = filters.studyImportLabel.trim();
        params.set(
          level === "patient" ? "study_import_label" : "import_label",
          v,
        );
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
      // Sidebar select-value quick filters live in filters.labelValues, keyed by
      // "<level>:<label>". Merge them into the same label_filters channel; union
      // with any column-header filter already set for the same label+level.
      if (filters.labelValues && typeof filters.labelValues === "object") {
        for (const [key, raw] of Object.entries(filters.labelValues)) {
          const values = normalizeSelectFilterValues(raw);
          if (values.length === 0) continue;
          const sep = key.indexOf(":");
          if (sep < 1) continue;
          const lvl = key.slice(0, sep);
          const label = key.slice(sep + 1);
          const existing = labelFilters.find(
            (f) =>
              f.datatype === "select" && f.label === label && f.level === lvl,
          );
          if (existing) {
            existing.values = [...new Set([...existing.values, ...values])];
          } else {
            labelFilters.push({
              label,
              level: lvl,
              values,
              datatype: "select",
            });
          }
        }
      }
      if (labelFilters.length > 0) {
        params.set("label_filters", JSON.stringify(labelFilters));
      }
      // Auto-column sidebar quick filters, as repeated params the API ORs
      // (?series_type=NCCT&series_type=CTA). Appended, not set, so they union
      // with any column-header filter already set on the same field.
      if (filters.autoValues && typeof filters.autoValues === "object") {
        for (const [field, raw] of Object.entries(filters.autoValues)) {
          for (const v of normalizeSelectFilterValues(raw))
            params.append(field, v);
        }
      }
      return params;
    },
    [level, config, filters, sortBy, sortDir, columnFilters, allCols],
  );

  // Fetch pages [from..to] sequentially. mode "replace" swaps the list,
  // "append" concatenates onto it. A monotonic seq token discards any response
  // whose request was superseded by a newer reset/loadMore.
  const fetchPages = useCallback(
    async (from, to, mode) => {
      const seq = ++seqRef.current;
      loadingRef.current = true;
      setLoading(true);
      try {
        const collected = [];
        let lastTotal = 0;
        for (let p = from; p <= to; p++) {
          const data = await apiGet(`${config.endpoint}?${buildParams(p)}`);
          if (seqRef.current !== seq) return;
          collected.push(...(data[config.itemsKey] || []));
          lastTotal = data.total ?? 0;
        }
        if (mode === "append") {
          setItems((prev) => {
            const next = prev.concat(collected);
            itemsLenRef.current = next.length;
            return next;
          });
        } else {
          setItems(collected);
          itemsLenRef.current = collected.length;
        }
        setTotal(lastTotal);
        totalRef.current = lastTotal;
      } catch {
        if (seqRef.current !== seq) return;
        if (mode !== "append") {
          setItems([]);
          itemsLenRef.current = 0;
          setTotal(0);
          totalRef.current = 0;
        }
      } finally {
        if (seqRef.current === seq) {
          loadingRef.current = false;
          setLoading(false);
        }
      }
    },
    [buildParams, config],
  );

  const reset = useCallback(() => {
    pageRef.current = 1;
    setResetNonce((n) => n + 1);
    fetchPages(1, 1, "replace");
  }, [fetchPages]);

  const loadMore = useCallback(() => {
    if (loadingRef.current) return;
    if (totalRef.current && itemsLenRef.current >= totalRef.current) return;
    pageRef.current += 1;
    fetchPages(pageRef.current, pageRef.current, "append");
  }, [fetchPages]);

  const reload = useCallback(() => {
    fetchPages(1, pageRef.current, "replace");
  }, [fetchPages]);

  // The only effect that auto-fires fetches. Keyed on the serialized page-1
  // query, NOT on callback identity: after an annotation save handleMutated
  // refetches label definitions, which rebuilds allCols with identical content
  // but a fresh identity. That churn must not reset the list — a reset
  // collapses the accumulated pages to page 1 and scrolls the table to the
  // top. Only a change in the effective query (filter/sort/level/columnFilter)
  // resets; everything else goes through reload(), which replaces in place.
  const querySig = `${config.endpoint}?${buildParams(1)}`;
  useEffect(() => {
    reset();
    // Deliberately keyed on querySig only (see note above); reset's identity
    // must not re-trigger. Disable sits on the deps line so prettier's
    // multi-line formatting keeps it aligned with the reported warning.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [querySig]);

  return {
    items,
    total,
    loading,
    hasMore: items.length < total,
    loadMore,
    reload,
    resetNonce,
  };
}
