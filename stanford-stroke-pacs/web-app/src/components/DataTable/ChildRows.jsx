import { Fragment } from "react";
import PropTypes from "prop-types";
import InlineEdit from "../InlineEdit";
import WarmButton from "./WarmButton";
import CopyPathButtons from "./CopyPathButtons";
import { isNarrowCol } from "../../utils/table";
import BuiltinCell from "./BuiltinCell";

const DownloadIcon = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{ display: "inline-block", verticalAlign: "middle" }}
  >
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
);

const TrashIcon = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{ display: "inline-block", verticalAlign: "middle" }}
  >
    <polyline points="3 6 5 6 21 6" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    <line x1="10" y1="11" x2="10" y2="17" />
    <line x1="14" y1="11" x2="14" y2="17" />
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
  canWarm,
  seriesStatus,
  onWarmSeries,
  onGrandChildRowClick,
  onResolveOhifLink,
  onDicomDownload,
  onRequestDelete,
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
      <td
        colSpan={gcColSpan}
        className="dt__gc-wrapper dt__gc-wrapper--level-series"
      >
        <div className="dt__gc-scroll">
          <table className="dt__gc-table">
            <thead className="dt__gc-thead">
              <tr className="dt__gc-head-row">
                {grandChildCols.map((c) => (
                  <th
                    key={c.key}
                    className={`dt__gc-th${isNarrowCol(c) ? " dt__gc-th--narrow" : ""}`}
                  >
                    {c.label}
                  </th>
                ))}
                <th className="dt__gc-th">Actions</th>
                <th
                  className="dt__gc-th dt__gc-th--spacer"
                  aria-hidden="true"
                />
              </tr>
            </thead>
            <tbody>
              {grandChildren.map((gc) => {
                const gcId = gc[grandChildConfig.idCol];
                const gcAnnotations = [
                  ...(gc.annotations || []),
                  ...(gc.inherited_annotations || []),
                ];
                const isActivePreview =
                  activeRowKey === `series:${gc.seriesinstanceuid}`;
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
                        return (
                          <td
                            key={c.key}
                            className={`dt__gc-td${isNarrowCol(c) ? " dt__gc-td--narrow" : ""}`}
                          >
                            <BuiltinCell col={c} row={gc} />
                          </td>
                        );
                      }
                      const labelName = c.key.replace("label:", "");
                      return (
                        <td
                          key={c.key}
                          className="dt__gc-td dt__gc-td--label"
                          onClick={(e) => e.stopPropagation()}
                        >
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
                    <td
                      className="dt__gc-td--actions"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <div className="dt__actions-inner dt__actions-inner--tight">
                        {gc.studyinstanceuid && (
                          <button
                            onClick={() =>
                              onResolveOhifLink(
                                gc.studyinstanceuid,
                                gc.seriesinstanceuid,
                              )
                            }
                            className="dt__gc-link-btn"
                          >
                            OHIF
                          </button>
                        )}
                        {gc.seriesinstanceuid && canWarm && (
                          <WarmButton
                            status={seriesStatus[gc.seriesinstanceuid]}
                            onWarm={() => onWarmSeries(gc.seriesinstanceuid)}
                            baseClass="dt__gc-link-btn"
                          />
                        )}
                        {gc.seriesinstanceuid && isAdmin && (
                          <>
                            <button
                              onClick={() =>
                                onDicomDownload(gc.seriesinstanceuid)
                              }
                              className="dt__gc-link-btn"
                              title="Download DICOM as zip"
                              disabled={
                                downloadingSeries === gc.seriesinstanceuid
                              }
                            >
                              {downloadingSeries === gc.seriesinstanceuid ? (
                                "\u2026"
                              ) : (
                                <DownloadIcon />
                              )}
                            </button>
                            <CopyPathButtons
                              seriesUid={gc.seriesinstanceuid}
                              baseClass="dt__gc-link-btn"
                            />
                          </>
                        )}
                        {gc.seriesinstanceuid && isAdmin && onRequestDelete && (
                          <button
                            onClick={() =>
                              onRequestDelete(
                                "series",
                                gc.seriesinstanceuid,
                                gc.seriesdescription,
                              )
                            }
                            className="dt__gc-link-btn dt__gc-link-btn--danger"
                            title="Delete this series"
                            aria-label="Delete this series"
                          >
                            <TrashIcon />
                          </button>
                        )}
                      </div>
                    </td>
                    <td
                      className="dt__gc-td dt__gc-td--spacer"
                      aria-hidden="true"
                    />
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

GrandChildTable.propTypes = {
  grandChildren: PropTypes.array,
  grandChildCols: PropTypes.array.isRequired,
  grandChildConfig: PropTypes.object.isRequired,
  gcColSpan: PropTypes.number.isRequired,
  activeRowKey: PropTypes.string,
  isAdmin: PropTypes.bool,
  downloadingSeries: PropTypes.string,
  canWarm: PropTypes.bool,
  seriesStatus: PropTypes.object,
  onWarmSeries: PropTypes.func,
  onGrandChildRowClick: PropTypes.func.isRequired,
  onResolveOhifLink: PropTypes.func.isRequired,
  onDicomDownload: PropTypes.func.isRequired,
  onRequestDelete: PropTypes.func,
  onMutated: PropTypes.func.isRequired,
};

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
  seriesStatus,
  onWarmStudy,
  onWarmSeries,
  onChildRowClick,
  onGrandChildRowClick,
  onResolveOhifLink,
  onDicomDownload,
  onRequestDelete,
  onMutated,
}) {
  const children = childRows[parentRowId];
  const childLevel =
    childConfig.idCol === "studyinstanceuid" ? "study" : "series";
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
          {childCols.map((c) => (
            <th
              key={c.key}
              className={`dt__child-th${isNarrowCol(c) ? " dt__child-th--narrow" : ""}`}
            >
              {c.label}
            </th>
          ))}
          <th className="dt__child-th">Actions</th>
          <th
            className="dt__child-th dt__child-th--spacer"
            aria-hidden="true"
          />
        </tr>
      </thead>
      <tbody>
        {children.map((child) => {
          const childId = child[childConfig.idCol];
          const gcKey = `${parentRowId}::${childId}`;
          const isGrandExpanded = grandExpanded[gcKey];
          const childAnnotations = [
            ...(child.annotations || []),
            ...(child.inherited_annotations || []),
          ];
          const childPreviewKey =
            childConfig.idCol === "seriesinstanceuid"
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
                    <span
                      className={`dt__child-expand-arrow${isGrandExpanded ? " dt__child-expand-arrow--open" : ""}`}
                    >
                      {"\u25B6"}
                    </span>
                  </td>
                )}
                {childCols.map((c) => {
                  if (c.builtin) {
                    return (
                      <td
                        key={c.key}
                        className={`dt__child-td${isNarrowCol(c) ? " dt__child-td--narrow" : ""}`}
                      >
                        <BuiltinCell col={c} row={child} />
                      </td>
                    );
                  }
                  const labelName = c.key.replace("label:", "");
                  return (
                    <td
                      key={c.key}
                      className="dt__child-td dt__child-td--label"
                      onClick={(e) => e.stopPropagation()}
                    >
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
                <td
                  className="dt__child-td--actions"
                  onClick={(e) => e.stopPropagation()}
                >
                  <div className="dt__actions-inner">
                    {child.studyinstanceuid && (
                      <button
                        onClick={() =>
                          onResolveOhifLink(
                            child.studyinstanceuid,
                            childConfig.idCol === "seriesinstanceuid"
                              ? child.seriesinstanceuid
                              : null,
                          )
                        }
                        className="link-btn"
                      >
                        OHIF
                      </button>
                    )}
                    {childConfig.idCol === "studyinstanceuid" &&
                      child.studyinstanceuid &&
                      canWarm && (
                        <WarmButton
                          status={studyStatus[child.studyinstanceuid]}
                          onWarm={() => onWarmStudy(child.studyinstanceuid)}
                        />
                      )}
                    {childConfig.idCol === "seriesinstanceuid" &&
                      child.seriesinstanceuid &&
                      canWarm && (
                        <WarmButton
                          status={seriesStatus[child.seriesinstanceuid]}
                          onWarm={() => onWarmSeries(child.seriesinstanceuid)}
                        />
                      )}
                    {childConfig.idCol === "seriesinstanceuid" &&
                      child.seriesinstanceuid &&
                      isAdmin && (
                        <>
                          <button
                            onClick={() =>
                              onDicomDownload(child.seriesinstanceuid)
                            }
                            className="link-btn"
                            title="Download DICOM as zip"
                            disabled={
                              downloadingSeries === child.seriesinstanceuid
                            }
                          >
                            {downloadingSeries === child.seriesinstanceuid ? (
                              "\u2026"
                            ) : (
                              <DownloadIcon />
                            )}
                          </button>
                          <CopyPathButtons
                            seriesUid={child.seriesinstanceuid}
                            baseClass="link-btn"
                          />
                        </>
                      )}
                    {isAdmin &&
                      onRequestDelete &&
                      (childConfig.idCol === "studyinstanceuid"
                        ? child.studyinstanceuid
                        : child.seriesinstanceuid) && (
                        <button
                          onClick={() =>
                            childConfig.idCol === "studyinstanceuid"
                              ? onRequestDelete(
                                  "study",
                                  child.studyinstanceuid,
                                  child.studydescription,
                                )
                              : onRequestDelete(
                                  "series",
                                  child.seriesinstanceuid,
                                  child.seriesdescription,
                                )
                          }
                          className="link-btn link-btn--danger"
                          title={`Delete this ${childLevel}`}
                          aria-label={`Delete this ${childLevel}`}
                        >
                          <TrashIcon />
                        </button>
                      )}
                  </div>
                </td>
                <td
                  className="dt__child-td dt__child-td--spacer"
                  aria-hidden="true"
                />
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
                  canWarm={canWarm}
                  seriesStatus={seriesStatus}
                  onWarmSeries={onWarmSeries}
                  onGrandChildRowClick={onGrandChildRowClick}
                  onResolveOhifLink={onResolveOhifLink}
                  onDicomDownload={onDicomDownload}
                  onRequestDelete={onRequestDelete}
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
      <td
        colSpan={parentColSpan}
        className={`dt__child-wrapper dt__child-wrapper--level-${childLevel}`}
      >
        {needsScroll ? (
          <div className="dt__child-scroll">{childTableContent}</div>
        ) : (
          childTableContent
        )}
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
  seriesStatus: PropTypes.object,
  onWarmStudy: PropTypes.func,
  onWarmSeries: PropTypes.func,
  onChildRowClick: PropTypes.func.isRequired,
  onGrandChildRowClick: PropTypes.func.isRequired,
  onResolveOhifLink: PropTypes.func.isRequired,
  onDicomDownload: PropTypes.func.isRequired,
  onRequestDelete: PropTypes.func,
  onMutated: PropTypes.func.isRequired,
};

export { DownloadIcon, TrashIcon };
