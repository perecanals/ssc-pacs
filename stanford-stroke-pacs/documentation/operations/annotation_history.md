# Annotation audit trail

**Purpose:** Every INSERT, UPDATE, and DELETE on the `annotations` table is
captured in `annotations_history` — an append-only audit log.  This provides
a complete change trail for reproducibility, forensic analysis, and regulatory
compliance.

---

## Schema

```text
annotations_history
  history_id       BIGSERIAL PRIMARY KEY
  operation        CHAR(1) NOT NULL         -- I = insert, U = update, D = delete
  operation_at     TIMESTAMPTZ DEFAULT now()
  operation_by     TEXT DEFAULT 'system'     -- authenticated username or 'system'
  annotation_id    INTEGER NOT NULL          -- FK to annotations.id (logical, not enforced)
  level            TEXT NOT NULL             -- patient | study | series
  entity_id        TEXT NOT NULL             -- resolved from level (patient_id / studyinstanceuid / seriesinstanceuid)
  label            TEXT NOT NULL
  value_before     TEXT                      -- NULL on INSERT
  value_after      TEXT                      -- NULL on DELETE
  notes_before     TEXT                      -- NULL on INSERT
  notes_after      TEXT                      -- NULL on DELETE
  created_by       TEXT                      -- snapshot of annotations.created_by
```

Indexes:
- `annotations_history_annotation_id_idx` — `(annotation_id, operation_at DESC)`
- `annotations_history_entity_id_idx` — `(entity_id, operation_at DESC)`

---

## How it works

### Trigger-based capture

A PL/pgSQL trigger (`annotations_audit_trg`) fires **AFTER INSERT OR UPDATE
OR DELETE** on `annotations`.  It captures the old/new values and writes a
row to `annotations_history`.

This approach was chosen over app-level capture because:
- It is impossible to bypass — any SQL path (UPSERT, direct DELETE, etc.)
  is captured.
- The `INSERT ... ON CONFLICT ... DO UPDATE` pattern fires the correct
  trigger (INSERT on first write, UPDATE on conflict).
- No annotation CRUD code needs modification.

### Session-variable coupling

The trigger reads `current_setting('app.audit_user', true)` to attribute
each change to the authenticated user.  The Web App middleware sets this
via `SET LOCAL` on every database connection obtained through `get_conn()`:

1. The request middleware extracts the username from the JWT cookie.
2. It sets `db.audit_user_var` (a `contextvars.ContextVar`).
3. `get_conn()` reads the contextvar and executes
   `SET LOCAL app.audit_user = '<username>'` on the connection.
4. The trigger reads it.  If unset, it defaults to `'system'`.

`SET LOCAL` is scoped to the current transaction — it resets on COMMIT or
ROLLBACK, so pooled connections don't leak state.

---

## API endpoint

```
GET /api/annotations/{annotation_id}/history
```

- **Auth:** admin-only (checks `users.is_admin`; non-admins get 403).
- **Response:** JSON array of history rows, newest first.

Example:
```json
[
  {
    "history_id": 42,
    "operation": "U",
    "operation_at": "2026-04-15T14:30:00+00:00",
    "operation_by": "jsmith",
    "annotation_id": 7,
    "level": "patient",
    "entity_id": "P-0001",
    "label": "stroke_type",
    "value_before": "ischemic",
    "value_after": "hemorrhagic",
    "notes_before": null,
    "notes_after": "Corrected after re-review",
    "created_by": "jsmith"
  }
]
```

---

## Backfill

For annotations that existed before the trigger was installed, run the
one-time backfill script:

```bash
conda activate pacs
cd stanford-stroke-pacs

# Preview
python scripts/one_off/backfill_annotation_history.py --dry-run

# Apply
python scripts/one_off/backfill_annotation_history.py --execute
```

The script inserts one synthetic `I` row per annotation that has no history
rows.  It uses the annotation's `created_at` and `created_by` for attribution.
It is idempotent and safe to re-run.

---

## Retention policy

**Default: keep forever.**  Annotations are small (a few hundred bytes each)
and mutation frequency is low — the history table will never dominate disk
usage.

If pruning is ever needed:

1. **Partition by month:** Add range partitioning on `operation_at`.
2. **Drop old partitions:** `ALTER TABLE annotations_history DETACH PARTITION ...`
   followed by `DROP TABLE`.
3. Alternatively, a simple `DELETE FROM annotations_history WHERE operation_at < ...`
   works for one-off cleanup.

---

## Rollback

To remove the audit trail entirely:

```sql
DROP TRIGGER annotations_audit_trg ON annotations;
DROP FUNCTION annotations_audit();
DROP TABLE annotations_history;
```

Prefer the manual SQL above. The audit trail was added in revision
`0003_annotations_history`, but later revisions (`0004`–`0006`) stack on top
of it, so `alembic downgrade 0002_warming_started_at` would also roll back
every migration after `0003` — not just the history table. Only use the
Alembic path if you genuinely intend to unwind to the `0002` schema.

---

## Related

- [Data stores reference](../reference/data_stores.md) — full table schemas
- [Schema migrations workflow](schema_migrations.md) — adding new revisions
- Alembic revision: `web-app/alembic/versions/0003_annotations_history.py`
