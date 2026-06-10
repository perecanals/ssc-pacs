import { Fragment } from "react";
import PropTypes from "prop-types";
import InlineEdit from "../InlineEdit";
import WarmButton from "./WarmButton";
import { formatDatetime } from "../../utils/table";

const DownloadIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ display: "inline-block", verticalAlign: "middle" }}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
);

function GrandChildTable({
  grandChildren,
  grandChildCols,
  grandChildConfig,
  gcColSpan,
  activeRowKey,
  isAdmin,
  downloadingSeries,
  onGrandChildRowClick,
  onResolveOhifLink,
  onDicomDownload,
  onMutated,
}) {
  if (!grandChildren || grandChildren.length === 0) {
    return (
      <tr>
        <td colSpan={gcColSpan} className="dt__gc-empty">
          No series found
        </td>
      </tr>
    );
  }
  return (
    <tr>
      <td colSpan={gcColSpan} className="dt__gc-wrapper dt__gc-wrapper--level-series">
        <div className="dt__gc-scroll">
          <table className="dt__gc-table">
            <thead className="dt__gc-thead">
              <tr className="dt__gc-head-row">
                {grandChildCols.map((c) => {
                  const isNarrow = c.builtin && (c.sourceKey === "patient_id" || c.sourceKey === "stroke_date");
                  return (
                    <th key={c.key} className={`dt__gc-th${isNarrow ? " dt__gc-th--narrow" : ""}`}>{c.label}</th>
                  );
                })}
                <th className="dt__gc-th">Actions</th>
                <th className="dt__gc-th dt__gc-th--spacer" aria-hidden="true" />
              </tr>
            </thead>
            <tbody>
              {grandChildren.map((gc) => {
                const gcId = gc[grandChildConfig.idCol];
                const gcAnnotations = [...(gc.annotations || []), ...(gc.inherited_annotations || [])];
                const isActivePreview = activeRowKey === `series:${gc.seriesinstanceuid}`;
                return (
                  <tr
                    key={gcId}
                    className={`dt__gc-row dt__gc-row--level-series dt__gc-row--previewable ${
                      isActivePreview ? "dt__gc-row--active" : ""
                    }`}
                    onClick={() => onGrandChildRowClick(gc)}
                  >
                    {grandChildCols.map((c) => {
                      if (c.builtin) {
                        const raw = gc[c.sourceKey] ?? "";
                        const display = c.sourceKey === "acquisitiondatetime" ? formatDatetime(raw) : raw;
                        const isNarrow = c.sourceKey === "patient_id" || c.sourceKey === "stroke_date";
                        return <td key={c.key} className={`dt__gc-td${isNarrow ? " dt__gc-td--narrow" : ""}`}>{display}</td>;
                      }
                      const labelName = c.key.replace("label:", "");
                      return (
                        <td key={c.key} className="dt__gc-td dt__gc-td--label" onClick={(e) => e.stopPropagation()}>
                          <InlineEdit
                            level={c.level}
                            entity={gc}
                            labelName={labelName}
                            datatype={c.datatype}
                            defOptions={c.options || []}
                            annotations={gcAnnotations}
                            onMutated={onMutated}
                          />
                        </td>
                      );
                    })}
                    <td className="dt__gc-td--actions" onClick={(e) => e.stopPropagation()}>
                      {gc.studyinstanceuid && (
                        <button
                          onClick={() => onResolveOhifLink(gc.studyinstanceuid, gc.seriesinstanceuid)}
                          className="dt__gc-link-btn"
                        >
                          OHIF
                        </button>
                      )}
                      {gc.seriesinstanceuid && isAdmin && (
                        <button
                          onClick={() => onDicomDownload(gc.seriesinstanceuid)}
                          className="dt__gc-link-btn"
                          title="Download DICOM as zip"
                          disabled={downloadingSeries === gc.seriesinstanceuid}
                        >
                          {downloadingSeries === gc.seriesinstanceuid ? "\u2026" : <DownloadIcon />}
                        </button>
                      )}
                    </td>
                    <td className="dt__gc-td dt__gc-td--spacer" aria-hidden="true" />
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </td>
    </tr>
  );
}

export default function ChildRows({
  parentRowId,
  childRows,
  childConfig,
  childCols,
  childIsExpandable,
  parentColSpan,
  grandExpanded,
  grandChildRows,
  grandChildCols,
  grandChildConfig,
  gcColSpan,
  activeRowKey,
  isAdmin,
  downloadingSeries,
  canWarm,
  studyStatus,
  onWarmStudy,
  onChildRowClick,
  onGrandChildRowClick,
  onResolveOhifLink,
  onDicomDownload,
  onMutated,
}) {
  const children = childRows[parentRowId];
  const childLevel = childConfig.idCol === "studyinstanceuid" ? "study" : "series";
  if (!children || children.length === 0) {
    return (
      <tr>
        <td colSpan={parentColSpan} className="dt__child-empty">
          No child records found
        </td>
      </tr>
    );
  }
  const childTableContent = (
    <table className="dt__child-table">
      <thead className="dt__child-thead">
        <tr className="dt__child-head-row">
          {childIsExpandable && <th className="dt__child-th--expand" />}
          {childCols.map((c) => {
            const isNarrow = c.builtin && (c.sourceKey === "patient_id" || c.sourceKey === "stroke_date");
            return (
              <th key={c.key} className={`dt__child-th${isNarrow ? " dt__child-th--narrow" : ""}`}>
                {c.label}
              </th>
            );
          })}
          <th className="dt__child-th">Actions</th>
          <th className="dt__child-th dt__child-th--spacer" aria-hidden="true" />
        </tr>
      </thead>
      <tbody>
        {children.map((child) => {
          const childId = child[childConfig.idCol];
          const gcKey = `${parentRowId}::${childId}`;
          const isGrandExpanded = grandExpanded[gcKey];
          const childAnnotations = [...(child.annotations || []), ...(child.inherited_annotations || [])];
          const childPreviewKey = childConfig.idCol === "seriesinstanceuid"
            ? `series:${child.seriesinstanceuid}`
            : `study:${child.studyinstanceuid}`;
          const isActivePreview = activeRowKey === childPreviewKey;
          return (
            <Fragment key={childId}>
              <tr
                className={`dt__child-row dt__child-row--level-${childLevel}${
                  childIsExpandable ? " dt__child-row--expandable" : ""
                }${childConfig.idCol === "studyinstanceuid" || childConfig.idCol === "seriesinstanceuid" ? " dt__child-row--previewable" : ""}${
                  isActivePreview ? " dt__child-row--active" : ""
                }`}
                onClick={() => onChildRowClick(gcKey, child)}
              >
                {childIsExpandable && (
                  <td className="dt__child-expand-cell">
                    <span className={`dt__child-expand-arrow ${isGrandExpanded ? "rotate-90" : ""}`}>
                      {"\u25B6"}
                    </span>
                  </td>
                )}
                {childCols.map((c) => {
                  if (c.builtin) {
                    const raw = child[c.sourceKey] ?? "";
                    const display = c.sourceKey === "acquisitiondatetime" ? formatDatetime(raw) : raw;
                    const isNarrow = c.sourceKey === "patient_id" || c.sourceKey === "stroke_date";
                    return <td key={c.key} className={`dt__child-td${isNarrow ? " dt__child-td--narrow" : ""}`}>{display}</td>;
                  }
                  const labelName = c.key.replace("label:", "");
                  return (
                    <td key={c.key} className="dt__child-td dt__child-td--label" onClick={(e) => e.stopPropagation()}>
                      <InlineEdit
                        level={c.level}
                        entity={child}
                        labelName={labelName}
                        datatype={c.datatype}
                        defOptions={c.options || []}
                        annotations={childAnnotations}
                        onMutated={onMutated}
                      />
                    </td>
                  );
                })}
                <td className="dt__child-td--actions" onClick={(e) => e.stopPropagation()}>
                  {child.studyinstanceuid && (
                    <button
                      onClick={() => onResolveOhifLink(
                        child.studyinstanceuid,
                        childConfig.idCol === "seriesinstanceuid" ? child.seriesinstanceuid : null,
                      )}
                      className="link-btn"
                    >
                      OHIF
                    </button>
                  )}
                  {childConfig.idCol === "studyinstanceuid" && child.studyinstanceuid && canWarm && (
                    <WarmButton
                      status={studyStatus[child.studyinstanceuid]}
                      onWarm={() => onWarmStudy(child.studyinstanceuid)}
                    />
                  )}
                  {childConfig.idCol === "seriesinstanceuid" && child.seriesinstanceuid && isAdmin && (
                    <button
                      onClick={() => onDicomDownload(child.seriesinstanceuid)}
                      className="link-btn"
                      title="Download DICOM as zip"
                      disabled={downloadingSeries === child.seriesinstanceuid}
                    >
                      {downloadingSeries === child.seriesinstanceuid ? "\u2026" : <DownloadIcon />}
                    </button>
                  )}
                </td>
                <td className="dt__child-td dt__child-td--spacer" aria-hidden="true" />
              </tr>
              {childIsExpandable && isGrandExpanded && (
                <GrandChildTable
                  grandChildren={grandChildRows[gcKey]}
                  grandChildCols={grandChildCols}
                  grandChildConfig={grandChildConfig}
                  gcColSpan={gcColSpan}
                  activeRowKey={activeRowKey}
                  isAdmin={isAdmin}
                  downloadingSeries={downloadingSeries}
                  onGrandChildRowClick={onGrandChildRowClick}
                  onResolveOhifLink={onResolveOhifLink}
                  onDicomDownload={onDicomDownload}
                  onMutated={onMutated}
                />
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
  const needsScroll = !childIsExpandable;
  return (
    <tr>
      <td colSpan={parentColSpan} className={`dt__child-wrapper dt__child-wrapper--level-${childLevel}`}>
        {needsScroll
          ? <div className="dt__child-scroll">{childTableContent}</div>
          : childTableContent}
      </td>
    </tr>
  );
}

ChildRows.propTypes = {
  parentRowId: PropTypes.string.isRequired,
  childRows: PropTypes.object.isRequired,
  childConfig: PropTypes.object.isRequired,
  childCols: PropTypes.array.isRequired,
  childIsExpandable: PropTypes.bool.isRequired,
  parentColSpan: PropTypes.number.isRequired,
  grandExpanded: PropTypes.object.isRequired,
  grandChildRows: PropTypes.object.isRequired,
  grandChildCols: PropTypes.array.isRequired,
  grandChildConfig: PropTypes.object,
  gcColSpan: PropTypes.number.isRequired,
  activeRowKey: PropTypes.string,
  isAdmin: PropTypes.bool,
  downloadingSeries: PropTypes.string,
  canWarm: PropTypes.bool,
  studyStatus: PropTypes.object,
  onWarmStudy: PropTypes.func,
  onChildRowClick: PropTypes.func.isRequired,
  onGrandChildRowClick: PropTypes.func.isRequired,
  onResolveOhifLink: PropTypes.func.isRequired,
  onDicomDownload: PropTypes.func.isRequired,
  onMutated: PropTypes.func.isRequired,
};

export { DownloadIcon };
