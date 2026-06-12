import { useCallback, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { apiGet, apiPut } from "../api/client";
import { useAuth } from "../context/AuthContext";
import TopBar from "../components/TopBar";
import "./AdminUsers.css";

export default function AdminUsers() {
  const { isAdmin, loading: authLoading } = useAuth();
  const [users, setUsers] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [savingUser, setSavingUser] = useState(null);

  useEffect(() => {
    if (authLoading || !isAdmin) return undefined;
    let cancelled = false;
    Promise.all([apiGet("/api/admin/users"), apiGet("/api/datasets")])
      .then(([userRows, datasetList]) => {
        if (cancelled) return;
        setUsers(userRows);
        setDatasets(datasetList);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load users and datasets.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [authLoading, isAdmin]);

  const toggleDataset = useCallback(
    async (username, dataset) => {
      const user = users.find((u) => u.username === username);
      if (!user || user.is_admin || savingUser) return;
      const granted = user.allowed_datasets.includes(dataset);
      const next = granted
        ? user.allowed_datasets.filter((d) => d !== dataset)
        : [...user.allowed_datasets, dataset].sort();

      // Optimistic update; revert on failure.
      const previous = users;
      setUsers(
        users.map((u) =>
          u.username === username ? { ...u, allowed_datasets: next } : u,
        ),
      );
      setSavingUser(username);
      setError("");
      try {
        const res = await apiPut(
          `/api/admin/users/${encodeURIComponent(username)}/datasets`,
          { datasets: next },
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          setUsers(previous);
          setError(err.detail || `Could not update access for ${username}.`);
        }
      } catch {
        setUsers(previous);
        setError(`Could not update access for ${username}.`);
      } finally {
        setSavingUser(null);
      }
    },
    [users, savingUser],
  );

  if (!authLoading && !isAdmin) return <Navigate to="/" replace />;
  if (authLoading) return null;

  return (
    <div className="admin-users">
      <TopBar />
      <main className="admin-users__main">
        <header className="admin-users__header">
          <h1 className="admin-users__title">User Dataset Access</h1>
          <p className="admin-users__subtitle">
            Grant each user access to imaging datasets. Users without any
            grants see no data; admins always see everything.
          </p>
        </header>

        {error && <div className="admin-users__error">{error}</div>}

        {loading ? (
          <p className="admin-users__loading">Loading users…</p>
        ) : (
          <div className="admin-users__table-wrap">
            <table className="admin-users__table">
              <thead>
                <tr>
                  <th className="admin-users__th admin-users__th--user">User</th>
                  {datasets.map((ds) => (
                    <th key={ds} className="admin-users__th admin-users__th--dataset">
                      {ds}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr
                    key={u.username}
                    className={`admin-users__row${
                      savingUser === u.username ? " admin-users__row--saving" : ""
                    }`}
                  >
                    <td className="admin-users__td admin-users__td--user">
                      <span className="admin-users__username">{u.username}</span>
                      {u.is_admin && (
                        <span className="admin-users__admin-pill">admin</span>
                      )}
                    </td>
                    {u.is_admin ? (
                      <td
                        className="admin-users__td admin-users__td--all"
                        colSpan={datasets.length}
                      >
                        All datasets (admin)
                      </td>
                    ) : (
                      datasets.map((ds) => (
                        <td key={ds} className="admin-users__td admin-users__td--check">
                          <input
                            type="checkbox"
                            className="admin-users__checkbox"
                            checked={u.allowed_datasets.includes(ds)}
                            disabled={savingUser === u.username}
                            onChange={() => toggleDataset(u.username, ds)}
                            aria-label={`Grant ${u.username} access to ${ds}`}
                          />
                        </td>
                      ))
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
