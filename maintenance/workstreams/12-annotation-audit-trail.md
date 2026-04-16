# Workstream 12 — Annotation audit trail

**Status:** `todo`
**Priority:** `P2`
**Size:** `M` (≈ 3–4 days)
**Owner:** _(unassigned)_
**Dependencies:** **WS 04 (Alembic migrations)** — new table + trigger
should ship as a migration.

---

## 1. Context

Annotations are **hard-deleted** today (`companion/app.py:1521`) and
updates overwrite previous values with no record of what changed, when,
or by whom. For a research tool where annotations drive downstream
analysis, this is a reproducibility and integrity risk:

- If an analyst changes a label value a week after an export, there is
  no record the export was based on the old value.
- If a bug (or a user mistake) corrupts annotations, there is no
  forensic trail to reconstruct prior state.
- Regulatory posture around medical research data typically requires
  immutable audit trails.

This workstream adds an append-only history table capturing every
INSERT / UPDATE / DELETE on `annotations`.

See `AUDIT_FINDINGS.md` §4.6.

---

## 2. Scope

**In scope:**
- `annotations_history` table (append-only) with every change.
- Trigger-based capture (preferred) OR app-level capture — decide in
  T1.
- Admin-authenticated `GET /api/annotations/{id}/history` endpoint.
- A small UI affordance in `InlineEdit` to view history (optional,
  can be deferred to a follow-up).
- Documentation of retention policy.

**Out of scope:**
- Soft-delete with undelete UX (can be a follow-up; this workstream
  hard-deletes but records the deletion in history).
- Auditing other tables (label_definitions, user_preferences,
  snapshot_*). Can be added later using the same pattern.
- Tamper-proof / cryptographic audit logs (overkill for current scale).

---

## 3. Findings

- **F-12.1** — `DELETE FROM annotations` at `companion/app.py:1521`
  removes history.
- **F-12.2** — UPDATE paths (the UPSERT helper at
  `companion/app.py:1427–1461`) overwrite without recording the prior
  value.
- **F-12.3** — No `created_at` / `updated_at` diff history; only the
  latest value is stored.

---

## 4. Tasks

- [ ] **T1** — Decide trigger-based vs. app-level capture. **Preferred:
  trigger-based** — impossible to bypass, no code changes in app.
  App-level is simpler to test but can drift (a future code path
  forgets to log).
  Record the decision in PROGRESS notes.
- [ ] **T2** — Design `annotations_history` table:
  ```sql
  CREATE TABLE annotations_history (
    history_id       BIGSERIAL PRIMARY KEY,
    operation        CHAR(1) NOT NULL,   -- I | U | D
    operation_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation_by     TEXT,               -- username or 'system'
    annotation_id    BIGINT NOT NULL,
    level            TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    label_id         BIGINT NOT NULL,
    value_before     JSONB,              -- NULL on I
    value_after      JSONB,              -- NULL on D
    created_by       TEXT
  );
  CREATE INDEX annotations_history_annotation_id_idx
    ON annotations_history (annotation_id, operation_at DESC);
  ```
  Adjust column names to match the real `annotations` schema — read
  it first.
- [ ] **T3** — Write the Alembic revision (requires WS 04):
  - Create `annotations_history` table.
  - Create the `annotations_audit()` PL/pgSQL function.
  - Create trigger `annotations_audit_trg` on `annotations` for
    `INSERT OR UPDATE OR DELETE`.
  - Set `operation_by` via a session variable the app will set:
    `SET LOCAL app.current_user = '<username>';` at the start of each
    request's transaction. Fall back to `'system'` if unset.
- [ ] **T4** — In the Companion request middleware, set `app.current_user`
  on the transaction for authenticated requests. This is the lightest
  coupling possible between app and trigger. If WS 09 has not landed,
  add the hook in the current `app.py` dependency that wraps each
  request.
- [ ] **T5** — Implement `GET /api/annotations/{id}/history` returning
  the history rows sorted newest-first. Admin-only (same
  `is_admin` dependency used elsewhere). Include the
  `created_by` / `operation_by` attribution.
- [ ] **T6** — Backfill: insert one synthetic "I" row per existing
  annotation to establish a starting point. Run as a one-time script,
  not a migration (so it can be retried).
- [ ] **T7** — Add pytest coverage (requires WS 07):
  - Insert annotation → 1 history row with op `I`.
  - Update → 1 more row with op `U` and correct `value_before`.
  - Delete → 1 more row with op `D` and correct `value_before`.
  - `operation_by` is set from the session variable.
- [ ] **T8** — Document retention policy. Recommended default: **keep
  forever**. Annotations are small; history will never dominate disk.
  Document how to prune if ever needed (partition by month, drop old
  partitions).
- [ ] **T9** — Write
  `documentation/operations/annotation_history.md` (new) covering
  schema, retrieval procedure, and the session-variable coupling.
- [ ] **T10** — Optional: minimal UI affordance — a "history" link on
  an inline edit that fetches and renders the last 5 changes. If
  time-boxed, scope out.

---

## 5. Acceptance criteria

- [ ] Creating an annotation inserts a row in `annotations_history`
  with `operation='I'` and matching `value_after`.
- [ ] Updating overwrites the current value but appends a row with
  `operation='U'`, correct `value_before`, correct `value_after`,
  correct `operation_by`.
- [ ] Deleting removes the current row but appends a row with
  `operation='D'`, correct `value_before`.
- [ ] `GET /api/annotations/{id}/history` returns the history in
  descending time order. Non-admins get 403.
- [ ] Backfill produces one row per pre-existing annotation.
- [ ] Tests (T7) all green.

---

## 6. Verification

```bash
# Trigger-level sanity (via psql)
psql -d stanford-stroke -c \
  "INSERT INTO annotations (level, entity_id, label_id, value, created_by)
   VALUES ('patient', 'TEST', 1, '\"x\"', 'tester');"
psql -d stanford-stroke -c \
  "SELECT operation, operation_by, value_before, value_after
   FROM annotations_history WHERE entity_id = 'TEST';"
# Expect: I | tester | NULL | "x"

# App-level middleware sets user
# (inspect a request where you expect operation_by = <authenticated username>)

# API
curl -b cookies.txt http://localhost:8043/api/annotations/123/history | jq .

# Backfill
python stanford-stroke-pacs/scripts/one_off/backfill_annotation_history.py --dry-run
python stanford-stroke-pacs/scripts/one_off/backfill_annotation_history.py --execute
```

---

## 7. Rollback

- Drop the trigger: `DROP TRIGGER annotations_audit_trg ON annotations;`
- Drop the table: `DROP TABLE annotations_history;`
- Revert code changes (`git revert`).

The Alembic downgrade path should do the first two automatically.

---

## 8. Files touched

- `stanford-stroke-pacs/companion/alembic/versions/000N_annotations_history.py`
  (new)
- `stanford-stroke-pacs/companion/app.py` (edit — middleware hook, new
  endpoint) OR `companion/routes/annotations.py` if WS 09 landed
- `stanford-stroke-pacs/scripts/one_off/backfill_annotation_history.py` (new)
- `stanford-stroke-pacs/companion/tests/test_annotation_history.py`
  (new)
- `stanford-stroke-pacs/documentation/operations/annotation_history.md`
  (new)
- `stanford-stroke-pacs/documentation/reference/data_stores.md` (edit —
  add table description)
- `stanford-stroke-pacs/documentation/context.md` (edit — add link)
- `stanford-stroke-pacs/companion/src/components/InlineEdit.jsx` (optional
  edit if T10 is done)

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Trigger adds latency to annotation writes | low | low | Benchmark before/after; 10× the write cost is still <1 ms |
| Session variable not set → history rows attributed to 'system' | high | low | Middleware hook tested (T7); alert if 'system' ratio > threshold |
| History table grows unexpectedly large | low | low | Retention policy doc (T8); partition later if needed |
| Backfill fails partway | med | low | Backfill script is idempotent (use ON CONFLICT DO NOTHING on a dedup key) |
| Trigger conflicts with a future snapshot-rebuild DDL | low | med | `DROP TRIGGER` during migration, `CREATE` after |

---

## 10. Notes

- The `value_before`/`value_after` as JSONB keeps schema flexibility —
  the underlying value column is already JSON-shaped for multi-level
  annotations.
- The trigger-based approach is compatible with `INSERT ... ON
  CONFLICT ... DO UPDATE SET` (the UPSERT pattern in
  `_UPSERT_SQL`) — PostgreSQL fires the UPDATE trigger correctly.
- Consider extending to `label_definitions` once this pattern is
  proven. Not in scope here.
- If the team ever wants soft-delete + undelete UX, the history table
  already contains enough information to reconstruct prior state — a
  UI-only addition at that point.
