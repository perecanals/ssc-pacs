import PropTypes from "prop-types";
import SelectFilterControl from "./SelectFilterControl";
import { getTextFilterValue } from "../../utils/table";

export default function TableHeader({
  config,
  level,
  mainTableCols,
  showActions,
  frozenFirstCol,
  setFrozenFirstCol,
  sortBy,
  sortDir,
  columnFilters,
  dragOverKey,
  dropSide,
  dragColKeyRef,
  onSort,
  onColumnFilter,
  onBoolFilter,
  onSelectFilterToggle,
  onSelectFilterClear,
  onDragStart,
  onDragOver,
  onDragLeave,
  onDrop,
  onDragEnd,
}) {
  return (
    <thead className="dt__head">
      <tr>
        {config.expandable && (
          <th className={`dt__th--expand${frozenFirstCol ? " dt__th--expand-frozen" : ""}`} />
        )}
        {mainTableCols.map((c, idx) => (
          <th
            key={c.key}
            draggable
            onDragStart={(e) => onDragStart(c.key, e)}
            onDragOver={(e) => onDragOver(c.key, e)}
            onDragLeave={onDragLeave}
            onDrop={(e) => onDrop(c.key, e)}
            onDragEnd={onDragEnd}
            title={!c.builtin ? c.description || "" : undefined}
            onClick={() => c.builtin && c.sortable !== false && onSort(c.sourceKey)}
            className={`dt__th ${c.builtin && c.sortable !== false ? "dt__th--sortable" : ""}${
              frozenFirstCol && idx === 0
                ? config.expandable ? " dt__th--frozen-first-offset" : " dt__th--frozen-first"
                : ""
            }${dragColKeyRef.current === c.key ? " dt__th--dragging" : ""}${
              dragOverKey === c.key && dropSide === "before" ? " dt__th--drop-before" : ""
            }${dragOverKey === c.key && dropSide === "after" ? " dt__th--drop-after" : ""}${
              c.builtin && (c.sourceKey === "patient_id" || c.sourceKey === "stroke_date") ? " dt__th--narrow" : ""
            }`}
          >
            {c.label}
            {idx === 0 && (
              <button
                type="button"
                className={`dt__pin-btn${frozenFirstCol ? " dt__pin-btn--active" : ""}`}
                onClick={(e) => { e.stopPropagation(); setFrozenFirstCol((v) => !v); }}
                title={frozenFirstCol ? "Unfreeze column" : "Freeze column"}
              >
                {"\uD83D\uDCCC"}
              </button>
            )}
            {c.builtin && c.sortable !== false && sortBy === c.sourceKey && (
              <span className="dt__sort-arrow">
                {sortDir === "asc" ? "\u2191" : "\u2193"}
              </span>
            )}
            {!c.builtin && c.level && c.level !== level && (
              <span className="dt__level-hint">({c.level})</span>
            )}
          </th>
        ))}
        {showActions && (
          <th className="dt__th">
            Actions
          </th>
        )}
      </tr>
      <tr>
        {config.expandable && (
          <th className={`dt__filter-th${frozenFirstCol ? " dt__filter-th--expand-frozen" : ""}`} />
        )}
        {mainTableCols.map((c, idx) => (
          <th
            key={`f-${c.key}`}
            className={`dt__filter-th${
              frozenFirstCol && idx === 0
                ? config.expandable ? " dt__filter-th--frozen-first-offset" : " dt__filter-th--frozen-first"
                : ""
            }${c.builtin && (c.sourceKey === "patient_id" || c.sourceKey === "stroke_date") ? " dt__filter-th--narrow" : ""}`}
          >
            {c.builtin && config.filterParamMap[c.sourceKey] ? (
              <input
                type="text"
                placeholder="Filter\u2026"
                defaultValue={getTextFilterValue(columnFilters[c.sourceKey])}
                onChange={(e) => onColumnFilter(c.sourceKey, e.target.value)}
                className="dt__filter-input"
              />
            ) : !c.builtin && c.datatype === "bool" ? (
              <button
                type="button"
                className={`dt__bool-filter${columnFilters[c.key] ? " dt__bool-filter--active" : ""}`}
                onClick={() => onBoolFilter(c.key)}
                title={
                  columnFilters[c.key] === "true" ? "Showing: true \u2014 click for false"
                    : columnFilters[c.key] === "false" ? "Showing: false \u2014 click to clear"
                    : "Filter by bool value"
                }
              >
                {columnFilters[c.key] === "true" ? "\u2713"
                  : columnFilters[c.key] === "false" ? "\u2717"
                  : "\u2298"}
              </button>
            ) : !c.builtin && c.datatype === "select" ? (
              <SelectFilterControl
                options={c.options || []}
                value={columnFilters[c.key]}
                onToggle={(option) => onSelectFilterToggle(c.key, option)}
                onClear={() => onSelectFilterClear(c.key)}
              />
            ) : !c.builtin ? (
              <input
                type="text"
                placeholder="Filter\u2026"
                defaultValue={getTextFilterValue(columnFilters[c.key])}
                onChange={(e) => onColumnFilter(c.key, e.target.value)}
                className="dt__filter-input"
              />
            ) : null}
          </th>
        ))}
        {showActions && <th className="dt__filter-th" />}
      </tr>
    </thead>
  );
}

TableHeader.propTypes = {
  config: PropTypes.object.isRequired,
  level: PropTypes.string.isRequired,
  mainTableCols: PropTypes.array.isRequired,
  showActions: PropTypes.bool.isRequired,
  frozenFirstCol: PropTypes.bool.isRequired,
  setFrozenFirstCol: PropTypes.func.isRequired,
  sortBy: PropTypes.string.isRequired,
  sortDir: PropTypes.oneOf(["asc", "desc"]).isRequired,
  columnFilters: PropTypes.object.isRequired,
  dragOverKey: PropTypes.string,
  dropSide: PropTypes.oneOf(["before", "after"]),
  dragColKeyRef: PropTypes.object.isRequired,
  onSort: PropTypes.func.isRequired,
  onColumnFilter: PropTypes.func.isRequired,
  onBoolFilter: PropTypes.func.isRequired,
  onSelectFilterToggle: PropTypes.func.isRequired,
  onSelectFilterClear: PropTypes.func.isRequired,
  onDragStart: PropTypes.func.isRequired,
  onDragOver: PropTypes.func.isRequired,
  onDragLeave: PropTypes.func.isRequired,
  onDrop: PropTypes.func.isRequired,
  onDragEnd: PropTypes.func.isRequired,
};
