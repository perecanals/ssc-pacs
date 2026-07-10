# Schema migrations (Alembic)

Schema changes to the `stanford-stroke` PostgreSQL database are managed by
[Alembic](https://alembic.sqlalchemy.org/). One linear chain of revisions
lives at `stanford-stroke-pacs/alembic/versions/`.

> **Scope:** this Alembic project owns **only the `stanford-stroke` database**.
> The Orthanc database (`orthanc_db`) is managed by the Orthanc container and
> must not be touched.

---

## How it runs

- **App startup** — `init_db()` in `web-app/app.py` calls
  `alembic command.upgrade(cfg, "head")`. On a DB that is already at head
  this is a no-op (no DDL emitted, no transaction opened).
- **CLI** — from the stack root `stanford-stroke-pacs/`:
  ```bash
  conda activate ssc-pacs
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

Alembic lives at the **stack root** (a peer of `web-app/` and
`image_ingestion_protocols/`) because the schema is shared: the web-app applies
`upgrade head` at startup and the ingestion tests build scratch DBs from the same
tree. The web-app is the *runner*, not the owner.

```
stanford-stroke-pacs/
├── alembic.ini                  # CLI config; sqlalchemy.url is blank, env.py builds it
└── alembic/
    ├── env.py                   # builds DB URL from .env; include_object filter
    ├── script.py.mako           # template for `alembic revision`
    └── versions/
        ├── 0001_baseline.py     # snapshot of prod schema as of 2026-04-15
        ├── 0002_warming_started_at.py
        ├── 0003_annotations_history.py
        ├── 0004_label_def_instrument.py
        ├── 0005_users_must_change_password.py
        ├── 0006_create_patient_table.py
        ├── 0007_cache_state_queued_status.py
        ├── 0008_users_allowed_datasets.py
        ├── 0009_label_value_options.py
        ├── 0010_series_cache_state.py
        ├── 0011_image_table_indexes.py
        ├── 0012_upstream_size_columns.py
        ├── 0013_drop_snapshot_tables.py
        └── 0014_annotation_index_cleanup.py
```

The chain is linear (`0001` → `0014`). `alembic history` prints the live
graph; `alembic heads` should always show a single head.

---

## What lives where

The baseline (`0001_baseline.py`) creates **all** tables that existed in
production on 2026-04-15. They fall in three groups:

| Group | Tables | Managed by |
|---|---|---|
| web-app-owned | `annotations`, `label_definitions`, `users`, `user_preferences`, `series_cache_state` | Future Alembic revisions (the per-study `cache_state` and dead `orthanc_resource_map` were replaced/dropped by `0010_series_cache_state`) |
| Upstream raw | `patient`, `image_series`, `image_study`, `lvo_clinical_data` | External ingest pipeline (out of scope for Alembic; `patient` also has a `CREATE TABLE IF NOT EXISTS` bootstrap in revision `0006`) |
| Dynamic labelled mirrors | `image_series_labelled`, `image_study_labelled`, `patient_labelled` | `web-app/labelled_table_sync.py`, based on `label_definitions`. Refreshed **in the background after each annotation write** (eventually consistent — not in the request transaction) plus on demand via the "Refresh Labelled Tables" button, bulk-label scripts, and image ingest. (The `snapshot_*` tables that once lived here were dropped by `0013_drop_snapshot_tables`.) |

The upstream and dynamic groups are excluded from Alembic's `--autogenerate`
proposals via `include_object` in `alembic/env.py` — autogenerate will
not suggest `DROP TABLE image_series` just because no Alembic revision
mentions it.

The baseline still **creates** those tables so that `alembic upgrade head`
on a fresh empty DB produces a schema that matches production for the
schema-diff acceptance gate.

---

## Why the baseline is one combined revision (not one per pre-existing migration)

Production has been live for months; the old `INIT_SQL` + `MIGRATE_SQL`
blocks in `web-app/app.py` encoded the cumulative state, not a series
of individually re-runnable migrations (the dedup step in particular was
data-dependent and one-shot). Splitting them into separate Alembic
revisions would either replay one-shot DDL on prod or duplicate work.
The baseline captures the **terminal state** as a single revision and is
applied to prod via `alembic stamp` — no DDL re-runs.

---

## Adding a new schema change

```bash
cd stanford-stroke-pacs
conda activate ssc-pacs
set -a; . ../.env; set +a   # DB_USER / DB_PASSWORD for the psql/pg_dump calls below

# 1. Generate a new revision file (manually edit it — the project doesn't
#    use SQLAlchemy models, so --autogenerate will be empty).
alembic revision -m "add foo column to bar"

# 2. Edit alembic/versions/<rev>_add_foo_column_to_bar.py:
#    - upgrade()   — add the change
#    - downgrade() — best-effort reverse (or `op.execute("-- irreversible")`)

# 3. Test on a scratch DB:
psql -h localhost -U "$DB_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS stanford_stroke_scratch;"
psql -h localhost -U "$DB_USER" -d postgres \
    -c "CREATE DATABASE stanford_stroke_scratch;"
DB_NAME=stanford_stroke_scratch alembic upgrade head
DB_NAME=stanford_stroke_scratch alembic downgrade -1   # if reversible
DB_NAME=stanford_stroke_scratch alembic upgrade head

# 4. Verify schema diff against production (only `alembic_version` should differ).
#    Get prod to head first (prod trails repo head until the next web-app restart
#    applies the outstanding revisions), or the diff flags the un-applied ones:
pg_dump --schema-only --no-owner --no-privileges \
    -h localhost -U "$DB_USER" -d stanford-stroke > /tmp/prod-schema.sql
pg_dump --schema-only --no-owner --no-privileges \
    -h localhost -U "$DB_USER" -d stanford_stroke_scratch > /tmp/scratch-schema.sql
diff <(grep -v -E '^(\\restrict|\\unrestrict|-- Dumped|-- PostgreSQL)' /tmp/prod-schema.sql) \
     <(grep -v -E '^(\\restrict|\\unrestrict|-- Dumped|-- PostgreSQL)' /tmp/scratch-schema.sql)

# 5. Commit the revision file. The next web app restart applies it
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
- Touch only web-app-owned tables. Changes to upstream tables
  (`image_*`, `lvo_clinical_data`) belong in the external ingest project.

### Destructive downgrades — read before running `downgrade`

`alembic downgrade` is **not universally safe** on this chain. Some
`downgrade()` bodies destroy data or don't actually reverse their upgrade.
Know these before stepping backwards past them:

| Revision | `downgrade()` does | Hazard |
|---|---|---|
| `0006_create_patient_table` | `DROP TABLE public.patient CASCADE` | Drops the patient spine **and everything FK-referencing it** — full data loss for that table tree. |
| `0009_label_value_options` | `DROP TABLE public.label_value_options` | Loses every configured select-option; the labels revert to free-text. |
| `0010_series_cache_state` | recreates the old **study-keyed** `cache_state` table | Reintroduces the superseded per-study cache schema — incompatible with the current per-series `series_cache_state`. |
| `0013_drop_snapshot_tables` | `pass` (silent no-op) | Does **not** recreate the dropped `snapshot_*` tables — violates this doc's own "raise on irreversible" convention. Downgrading through it leaves the schema unchanged, so a subsequent re-upgrade is fine, but don't expect the snapshots back. |

For anything past these, prefer a fresh forward revision or a restore from
backup over a blind `downgrade`.

---

## Production rollout (one-time)

The first time Alembic is deployed to a live DB that already matches the
baseline, **stamp** the DB instead of running the upgrade:

```bash
cd /opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs
conda activate ssc-pacs

# 1. Backup first (see operations/backup_strategy.md).
# 2. Stamp:
alembic stamp 0001_baseline
# 3. Verify:
alembic current   # should print: 0001_baseline (head)
# 4. Restart web app. init_db() will see head and emit no DDL.
#    macOS: sudo launchctl kickstart -k system/com.ssc.webapp
sudo systemctl restart ssc-web-app                  # macOS: launchctl kickstart -k system/com.ssc.webapp
journalctl -u ssc-web-app -n 50                     # macOS: tail -n 50 ~/Library/Logs/ssc-web-app.err
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
  `journalctl -u ssc-web-app` (macOS: `~/Library/Logs/ssc-web-app.err`).
  Common cause: a new revision references
  a table or column that doesn't exist; rerun the test from §"Adding a new
  schema change".
- **Schema drift suspected** — re-run the diff procedure against prod.
  If a real drift is found, write a new revision to bring schema in line
  rather than editing an existing revision file.

---

## Rollback

- Single revision: `alembic downgrade -1`.
- Whole workstream: revert `web-app/app.py`, optionally
  `DROP TABLE alembic_version` to clean up. The pre-Alembic
  `INIT_SQL`/`MIGRATE_SQL` blocks were idempotent against the current prod
  schema, so reinstating them is safe.
- Catastrophic failure: restore from the backup taken before rollout
  (see `operations/restore_runbook.md`).
