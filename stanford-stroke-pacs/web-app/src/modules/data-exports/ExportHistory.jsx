import PropTypes from "prop-types";
import { ROOT } from "./api";

export default function ExportHistory({
  jobs,
  detail,
  busy,
  historyOffset,
  onPage,
  onRefresh,
  onDetails,
  onCloseDetails,
  onEdit,
  onCancel,
}) {
  return (
    <section className="data-exports__history">
      <div className="data-exports__actions">
        <h2>Export history</h2>
        <button className="btn-outline" disabled={busy} onClick={onRefresh}>
          Refresh
        </button>
      </div>
      <p>
        Audit history records who exported, when, and the exact query
        configuration. A rerun reads current data.
      </p>
      <div className="data-exports__results">
        <table>
          <thead>
            <tr>
              <th>Dataset</th>
              <th>Export name</th>
              <th>Requested</th>
              <th>User</th>
              <th>Format</th>
              <th>Status</th>
              <th>Rows written</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>
                  <strong>
                    {j.configuration?.dataset ||
                      (Array.isArray(j.configuration?.authorized_datasets)
                        ? j.configuration.authorized_datasets.join(", ") ||
                          "No datasets"
                        : "All datasets")}
                  </strong>
                </td>
                <td>{j.name}</td>
                <td>{new Date(j.created_at).toLocaleString()}</td>
                <td>{j.username}</td>
                <td>{j.format.toUpperCase()}</td>
                <td>
                  {j.status}
                  {j.error && <small>{j.error}</small>}
                </td>
                <td>{Number(j.row_count).toLocaleString()}</td>
                <td>
                  <div className="data-exports__actions">
                    <button
                      className="btn-outline"
                      disabled={busy}
                      onClick={() => onDetails(j.id)}
                    >
                      Details
                    </button>
                    <button
                      className="btn-outline"
                      disabled={busy}
                      onClick={() => onEdit(j)}
                    >
                      Edit export
                    </button>
                    {["running", "queued"].includes(j.status) && (
                      <button
                        className="btn-outline"
                        disabled={busy}
                        onClick={() => onCancel(j.id)}
                      >
                        Cancel
                      </button>
                    )}
                    {j.status === "completed" && (
                      <a
                        className="btn-outline"
                        href={`${ROOT}/exports/${j.id}/download`}
                      >
                        Download
                      </a>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!jobs.length && <p>No exports yet.</p>}
      <div className="data-exports__actions">
        <button
          className="btn-outline"
          disabled={!historyOffset}
          onClick={() => onPage(Math.max(0, historyOffset - 50))}
        >
          Newer exports
        </button>
        <button
          className="btn-outline"
          disabled={jobs.length < 50}
          onClick={() => onPage(historyOffset + 50)}
        >
          Older exports
        </button>
      </div>
      {detail && (
        <section className="data-exports__detail">
          <div className="data-exports__actions">
            <h3>Export details: {detail.name}</h3>
            <button className="btn-outline" onClick={onCloseDetails}>
              Close details
            </button>
          </div>
          <p>
            {detail.username} · {detail.status} ·{" "}
            {detail.file_size == null
              ? "Size pending"
              : `${Number(detail.file_size).toLocaleString()} bytes`}
          </p>
          <pre className="data-exports__sql-output">
            {detail.equivalent_sql}
          </pre>
          <details>
            <summary>Recorded configuration</summary>
            <pre>{JSON.stringify(detail.configuration, null, 2)}</pre>
          </details>
          <h4>Download requests</h4>
          {detail.downloads.map((d, i) => (
            <p key={i}>
              {d.username} · {new Date(d.requested_at).toLocaleString()}
            </p>
          ))}
          {!detail.downloads.length && <p>No download requests recorded.</p>}
        </section>
      )}
    </section>
  );
}

ExportHistory.propTypes = {
  jobs: PropTypes.array.isRequired,
  detail: PropTypes.object,
  busy: PropTypes.bool.isRequired,
  historyOffset: PropTypes.number.isRequired,
  onPage: PropTypes.func.isRequired,
  onRefresh: PropTypes.func.isRequired,
  onDetails: PropTypes.func.isRequired,
  onCloseDetails: PropTypes.func.isRequired,
  onEdit: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
};
