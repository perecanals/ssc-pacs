# Schema migrations (Alembic)

Schema changes to the `stanford-stroke` PostgreSQL database are managed by
[Alembic](https://alembic.sqlalchemy.org/). One linear chain of revisions
lives at `stanford-stroke-pacs/companion/alembic/versions/`.

> **Scope:** this Alembic project owns **only the `stanford-stroke` database**.
> The Orthanc database (`orthanc_db`) is managed by the Orthanc container and
> must not be touched.

---

## How it runs

- **App startup** — `init_db()` in `companion/app.py` calls
  `alembic command.upgrade(cfg, "head")`. On a DB that is already at head
  this is a no-op (no DDL emitted, no transaction opened).
- **CLI** — from `stanford-stroke-pacs/companion/`:
  ```bash
  conda activate pacs
  alembic current     # show the revision applied to this DB
  alembic history     # full revision graph
  alembic upgrade head
  alembic downgrade -1
  ```
- **Override the target DB** — set `DATABASE_URL=postgresql+psycopg2://…`
  or set `DB_NAME=…` (the rest comes from `.env`). Used for scratch DBs
  during testing.

---

## Files

```
stanford-stroke-pacs/companion/
├── alembic.ini                  # CLI config; sqlalchemy.url is blank, env.py builds it
└── alembic/
    ├── env.py                   # builds DB URL from .env; include_object filter
    ├── script.py.mako           # template for `alembic revision`
    └── versions/
        └── 0001_baseline.py     # snapshot of prod schema as of 2026-04-15
```

---

## What lives where

The baseline (`0001_baseline.py`) creates **all** tables that existed in
production on 2026-04-15. They fall in three groups:

| Group | Tables | Managed by |
|---|---|---|
| Companion-owned | `annotations`, `label_definitions`, `users`, `user_preferences`, `cache_state`, `orthanc_resource_map` | Future Alembic revisions |
| Upstream raw | `image_series`, `image_study`, `lvo_clinical_data` | External ingest pipeline (out of scope for Alembic) |
| Dynamic labelled / snapshot | `image_series_labelled`, `image_study_labelled`, `lvo_clinical_data_labelled`, `snapshot_patients`, `snapshot_studys`, `snapshot_seriess` | `companion/labelled_table_sync.py` at runtime, based on `label_definitions` |

The upstream and dynamic groups are excluded from Alembic's `--autogenerate`
proposals via `include_object` in `alembic/env.py` — autogenerate will
not suggest `DROP TABLE image_series` just because no Alembic revision
mentions it.

The baseline still **creates** those tables so that `alembic upgrade head`
on a fresh empty DB produces a schema that matches production for the
schema-diff acceptance gate (workstream 04 §6).

---

## Why the baseline is one combined revision (not one per pre-existing migration)

Production has been live for months; the old `INIT_SQL` + `MIGRATE_SQL`
blocks in `companion/app.py` encoded the cumulative state, not a series
of individually re-runnable migrations (the dedup step in particular was
data-dependent and one-shot). Splitting them into separate Alembic
revisions would either replay one-shot DDL on prod or duplicate work.
The baseline captures the **terminal state** as a single revision and is
applied to prod via `alembic stamp` — no DDL re-runs.

---

## Adding a new schema change

```bash
cd stanford-stroke-pacs/companion
conda activate pacs

# 1. Generate a new revision file (manually edit it — the project doesn't
#    use SQLAlchemy models, so --autogenerate will be empty).
alembic revision -m "add foo column to bar"

# 2. Edit alembic/versions/<rev>_add_foo_column_to_bar.py:
#    - upgrade()   — add the change
#    - downgrade() — best-effort reverse (or `op.execute("-- irreversible")`)

# 3. Test on a scratch DB:
PGPASSWORD=… psql -h localhost -U perecanals -d postgres \
    -c "DROP DATABASE IF EXISTS stanford_stroke_scratch;"
PGPASSWORD=… psql -h localhost -U perecanals -d postgres \
    -c "CREATE DATABASE stanford_stroke_scratch;"
DB_NAME=stanford_stroke_scratch alembic upgrade head
DB_NAME=stanford_stroke_scratch alembic downgrade -1   # if reversible
DB_NAME=stanford_stroke_scratch alembic upgrade head

# 4. Verify schema diff against production (only `alembic_version` should differ):
PGPASSWORD=… pg_dump --schema-only --no-owner --no-privileges \
    -h localhost -U perecanals -d stanford-stroke > /tmp/prod-schema.sql
PGPASSWORD=… pg_dump --schema-only --no-owner --no-privileges \
    -h localhost -U perecanals -d stanford_stroke_scratch > /tmp/scratch-schema.sql
diff <(grep -v -E '^(\\restrict|\\unrestrict|-- Dumped|-- PostgreSQL)' /tmp/prod-schema.sql) \
     <(grep -v -E '^(\\restrict|\\unrestrict|-- Dumped|-- PostgreSQL)' /tmp/scratch-schema.sql)

# 5. Commit the revision file. The next companion restart applies it
#    automatically via init_db().
```

### Conventions

- One revision per logical change. Don't bundle unrelated DDL.
- Always wrap the change so re-running on an upgraded DB is a no-op
  (Alembic's per-revision transaction takes care of all-or-nothing; the
  no-op behavior is automatic because Alembic skips revisions whose
  `version_num` is already in `alembic_version`).
- Provide a `downgrade()` when feasible. For irreversible changes
  (data destruction, complex backfills), set the body to:
  ```python
  def downgrade() -> None:
      raise NotImplementedError("Migration <NNNN> is irreversible: <reason>")
  ```
- Touch only Companion-owned tables. Changes to upstream tables
  (`image_*`, `lvo_clinical_data`) belong in the external ingest project.

---

## Production rollout (one-time)

The first time Alembic is deployed to a live DB that already matches the
baseline, **stamp** the DB instead of running the upgrade:

```bash
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs/companion
conda activate pacs

# 1. Backup first (see operations/backup_strategy.md).
# 2. Stamp:
alembic stamp 0001_baseline
# 3. Verify:
alembic current   # should print: 0001_baseline (head)
# 4. Restart companion. init_db() will see head and emit no DDL.
sudo systemctl restart ssc-companion
sudo journalctl -u ssc-companion -n 50 --no-pager
```

If a future deployment needs to apply outstanding revisions (the normal
case), `init_db()` runs `alembic upgrade head` automatically at startup —
no manual steps required.

---

## Troubleshooting

- **`alembic current` prints nothing** — the DB has no `alembic_version`
  table yet. Either you forgot the `stamp` step, or this is a fresh
  scratch DB (run `alembic upgrade head`).
- **`init_db()` fails on startup** — read the traceback from
  `journalctl -u ssc-companion`. Common cause: a new revision references
  a table or column that doesn't exist; rerun the test from §"Adding a new
  schema change".
- **Schema drift suspected** — re-run the diff procedure against prod.
  If a real drift is found, write a new revision to bring schema in line
  rather than editing an existing revision file.

---

## Rollback

- Single revision: `alembic downgrade -1`.
- Whole workstream: revert `companion/app.py`, optionally
  `DROP TABLE alembic_version` to clean up. The pre-Alembic
  `INIT_SQL`/`MIGRATE_SQL` blocks were idempotent against the current prod
  schema, so reinstating them is safe.
- Catastrophic failure: restore from the backup taken before rollout
  (see `operations/restore_runbook.md`).
