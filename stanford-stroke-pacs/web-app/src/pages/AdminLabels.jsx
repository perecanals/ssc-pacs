import { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import { apiGet, apiPut } from "../api/client";
import { useAuth } from "../context/AuthContext";
import TopBar from "../components/TopBar";
import { groupByInstrument } from "../utils/table";
import "./AdminLabels.css";

// Sibling of AdminUsers: that page grants which cohorts a user may *see*, this
// one grants which labels a user may *write*.
export default function AdminLabels() {
  const { isAdmin, loading: authLoading } = useAuth();
  const [labels, setLabels] = useState([]);
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [savingId, setSavingId] = useState(null);

  useEffect(() => {
    if (authLoading || !isAdmin) return undefined;
    let cancelled = false;
    Promise.all([
      apiGet("/api/admin/label-definitions"),
      apiGet("/api/admin/users"),
    ])
      .then(([labelRows, userRows]) => {
        if (cancelled) return;
        setLabels(labelRows);
        setUsers(userRows);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load labels and users.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [authLoading, isAdmin]);

  // Every user is a candidate editor, admins included: there is no admin
  // bypass, so an admin who needs to edit a restricted label must be listed.
  const usernames = useMemo(() => users.map((u) => u.username).sort(), [users]);

  // Same grouping and ordering as the sidebar and the Displayed Columns menu
  // (instruments alphabetical, Unassigned last; members by creation time), so a
  // label sits in the same place wherever you look for it. Derived from
  // `labels`, so optimistic updates flow through without extra bookkeeping.
  const groups = useMemo(() => groupByInstrument(labels), [labels]);

  const save = useCallback(
    async (labelId, editPolicy, editUsers) => {
      if (savingId) return;
      const previous = labels;
      setLabels(
        labels.map((l) =>
          l.id === labelId
            ? { ...l, edit_policy: editPolicy, edit_users: editUsers }
            : l,
        ),
      );
      setSavingId(labelId);
      setError("");
      const name = previous.find((l) => l.id === labelId)?.name || labelId;
      try {
        const res = await apiPut(
          `/api/admin/label-definitions/${encodeURIComponent(labelId)}/permissions`,
          { edit_policy: editPolicy, edit_users: editUsers },
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          setLabels(previous);
          setError(err.detail || `Could not update who can edit ${name}.`);
        }
      } catch {
        setLabels(previous);
        setError(`Could not update who can edit ${name}.`);
      } finally {
        setSavingId(null);
      }
    },
    [labels, savingId],
  );

  const changePolicy = (label, policy) => {
    // Switching to "users" needs at least one name (the server rejects an empty
    // list — it would silently mean "nobody"). Seed with the existing list, or
    // the owner when they are a real user; otherwise wait for a checkbox.
    if (policy === "users") {
      const seed = label.edit_users.length
        ? label.edit_users
        : usernames.includes(label.created_by)
          ? [label.created_by]
          : [];
      if (!seed.length) {
        setError(`Pick at least one user who can edit ${label.name}.`);
        // Reflect the intent locally so the checkboxes appear; nothing is saved
        // until a name is ticked.
        setLabels(
          labels.map((l) =>
            l.id === label.id
              ? { ...l, edit_policy: "users", edit_users: [] }
              : l,
          ),
        );
        return;
      }
      save(label.id, "users", seed);
      return;
    }
    save(label.id, policy, []);
  };

  const toggleUser = (label, username) => {
    const next = label.edit_users.includes(username)
      ? label.edit_users.filter((u) => u !== username)
      : [...label.edit_users, username].sort();
    if (!next.length) {
      // An empty list is not a valid "users" policy; that intent is "nobody".
      save(label.id, "nobody", []);
      return;
    }
    save(label.id, "users", next);
  };

  if (!authLoading && !isAdmin) return <Navigate to="/" replace />;
  if (authLoading) return null;

  return (
    <div className="admin-labels">
      <TopBar />
      <main className="admin-labels__main">
        <header className="admin-labels__header">
          <h1 className="admin-labels__title">Label Access</h1>
          <p className="admin-labels__subtitle">
            Choose who can edit each label&apos;s values. Locking a label makes
            its cells read-only for everyone — including admins — so bulk-loaded
            data is not overwritten by a stray click. To correct a locked value,
            set it back to Everyone, edit, then lock it again.
          </p>
        </header>

        {error && <div className="admin-labels__error">{error}</div>}

        {loading ? (
          <p className="admin-labels__loading">Loading labels…</p>
        ) : (
          <div className="admin-labels__table-wrap">
            <table className="admin-labels__table">
              <thead>
                <tr>
                  <th className="admin-labels__th">Label</th>
                  <th className="admin-labels__th">Level</th>
                  <th className="admin-labels__th">Owner</th>
                  <th className="admin-labels__th">Who can edit values</th>
                </tr>
              </thead>
              {groups.map((g) => (
                <tbody key={g.key} className="admin-labels__group">
                  <tr>
                    <th
                      className="admin-labels__instrument-header"
                      colSpan={4}
                      scope="colgroup"
                    >
                      {g.name}{" "}
                      <span className="admin-labels__instrument-count">
                        ({g.items.length})
                      </span>
                    </th>
                  </tr>
                  {g.items.map((l) => (
                    <tr
                      key={l.id}
                      className={`admin-labels__row${
                        savingId === l.id ? " admin-labels__row--saving" : ""
                      }`}
                    >
                      <td className="admin-labels__td">
                        <span className="admin-labels__name">{l.name}</span>
                      </td>
                      <td className="admin-labels__td admin-labels__td--muted">
                        {l.level}
                      </td>
                      <td className="admin-labels__td admin-labels__td--muted">
                        {l.created_by}
                      </td>
                      <td className="admin-labels__td">
                        <select
                          className="admin-labels__select"
                          value={l.edit_policy}
                          disabled={savingId === l.id}
                          onChange={(e) => changePolicy(l, e.target.value)}
                          aria-label={`Who can edit ${l.name}`}
                        >
                          <option value="everyone">Everyone</option>
                          <option value="users">Selected users</option>
                          <option value="nobody">No one</option>
                        </select>
                        {l.edit_policy === "users" && (
                          <div className="admin-labels__users">
                            {usernames.map((u) => (
                              <label key={u} className="admin-labels__user">
                                <input
                                  type="checkbox"
                                  className="admin-labels__checkbox"
                                  checked={l.edit_users.includes(u)}
                                  disabled={savingId === l.id}
                                  onChange={() => toggleUser(l, u)}
                                  aria-label={`Let ${u} edit ${l.name}`}
                                />
                                {u}
                              </label>
                            ))}
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              ))}
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
