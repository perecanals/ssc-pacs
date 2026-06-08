# Data stores (PostgreSQL)

**Purpose:** Authoritative reference for logical databases, source tables, web-app-owned tables, and optional cold-storage schema. For query/join behavior at a glance, see also [How the web app queries the DB](#how-the-web app-queries-the-db). Stack context: [`architecture.md`](architecture.md).

This PACS stack uses **one PostgreSQL server** but **two logical databases** with separate responsibilities:

- **`orthanc_db`**: Orthanc’s internal index database (managed by Orthanc’s PostgreSQL plugin).
- **`stanford-stroke`**: Research / application database used by the web app and helper scripts.

---

## `orthanc_db` (Orthanc index DB)

Owned and mutated by Orthanc only.

- **Purpose**: operational indexing for Orthanc Explorer 2, OHIF, DICOMweb, and REST lookups.
- **Data source**: Orthanc Folder Indexer scans the on-disk DICOM tree and writes metadata into this DB.
- **Storage model**: the canonical DICOM files remain on disk; Orthanc stores *index/metadata*, not image payloads.

You generally should not query or migrate Orthanc tables directly unless you are doing explicit Orthanc-specific work (e.g. optional enrichment scripts that intentionally mutate Orthanc’s index tables).

Connection is configured via `ORTHANC__POSTGRESQL__*` in `docker-compose.yml` (see [`runtime_and_config.md`](runtime_and_config.md)).

---

## `stanford-stroke` (research / app DB)

This is where the web app reads metadata and stores annotations and preferences.

### Source metadata tables (upstream-owned)

These tables drive browsing in the Navigator UI:

- **`patient`** (patient-level registry — the patient-tab spine)
  - one row per patient in the database, populated equivalently to
    `image_study`/`image_series` by the ingest pipeline (idempotent upsert) and
    backfillable from imaging. Comprehensive: a patient appears here whether or
    not a clinical row exists in `lvo_clinical_data`.
  - fields: `patient_id` (PK), `stroke_date` (imaging-derived =
    `MIN(image_study.acquisitiondatetime)`), `import_id`/`import_label` (origin
    batch, preserved on conflict), `dataset` (`text[]`, union-accumulated),
    `created_at`, `updated_at`
- **`lvo_clinical_data`** (clinical side-table — *not* the patient spine)
  - clinical variables (demographics, outcomes, etc.). The patient tab joins it
    only to prefer its `stroke_date` when a patient is clinically matched.
  - key fields: `study_id` (the patient id; joined as `c.study_id = patient.patient_id`), `stroke_date` (TEXT)
- **`image_study`** (study-level imaging metadata)
  - typical fields: `patient_id`, `studyinstanceuid`, `studydescription`, `study_type`, `study_path`, `acquisitiondatetime`, `import_id`, `import_label`
- **`image_series`** (series-level imaging metadata)
  - typical fields: `patient_id`, `studyinstanceuid`, `seriesinstanceuid`, `seriesdescription`, `modality`, `acquisitiondatetime`
  - file pointers: `dicom_dir_path`, `nifti_path`
  - optional cold storage: **`dicom_archive_path`** — path to per-series `*.tar.zst` when using `cold_path_cache` mode
  - ingestion bookkeeping: `import_id`, `import_label`
  - geometry-derived: `imageshape`, **`number_of_slices`**

Notes:

- The legacy Stanford ingestion pipeline (`image_integration_protocols/`) upserts into `image_study` and `image_series`.
- `number_of_slices` is populated during ingest and can be backfilled for existing rows.

### web-app-owned tables (app-managed)

These tables are created and evolved by **Alembic migrations** under
`web-app/alembic/versions/`. `init_db()` runs `alembic upgrade head` on
startup. See [`../operations/schema_migrations.md`](../operations/schema_migrations.md)
for the workflow when adding a new revision.

- **`users`**: the single source of truth for end-user authentication (bcrypt password hashes). The `is_admin` column gates `/api/admin/*` endpoints and the "Orthanc Explorer" Landing card; admins are also mirrored into `orthanc_users.json` by `scripts/admin/manage_users.py` so they can reach `:8042` directly.
- **`annotations`**: multi-level (patient / study / series) annotations.
- **`label_definitions`**: label registry (level-aware; supports bool/int/text/select).
- **`user_preferences`**: per-user persisted table layout/state (JSONB).
- **`snapshot_patients` / `snapshot_studys` / `snapshot_seriess`**: refreshable export-oriented snapshot tables.

Audit:

- **`annotations_history`**: append-only audit trail for every annotation change (trigger-captured). See [`../operations/annotation_history.md`](../operations/annotation_history.md).

Cold storage / hot cache (when enabled):

- **`cache_state`**: per-study warm status and paths for the hot cache.
- **`orthanc_resource_map`**: Orthanc resource IDs linked to warmed studies (supports eviction).

### `image_series.dicom_archive_path`

Nullable `TEXT`. Populated by the offline archiver (`scripts/cold_storage/archive_all_series.py`) when series are packed to `*.tar.zst`. Used when `[storage].mode = "cold_path_cache"` in `config.toml`.

### `cache_state`

```text
studyinstanceuid   TEXT PRIMARY KEY
status             TEXT NOT NULL DEFAULT 'cold'
                   CHECK (status IN ('cold', 'warming', 'hot', 'error'))
cache_path         TEXT
warmed_at          TIMESTAMPTZ
last_accessed_at   TIMESTAMPTZ
error_message      TEXT
```

Indexes: `idx_cache_state_status`, `idx_cache_state_last_accessed`.

### `orthanc_resource_map`

```text
orthanc_id         TEXT PRIMARY KEY
resource_type      TEXT NOT NULL CHECK (resource_type IN ('study', 'series', 'instance'))
studyinstanceuid   TEXT NOT NULL REFERENCES cache_state(studyinstanceuid) ON DELETE CASCADE
seriesinstanceuid  TEXT
created_at         TIMESTAMPTZ DEFAULT now()
```

Index: `idx_orm_study` on `studyinstanceuid`.

---

## Web App table DDL (logical reference)

Migrations in `web-app/app.py` are authoritative; this section mirrors the intended shape for documentation readers.

### `annotations`

```text
id                  SERIAL PRIMARY KEY
seriesinstanceuid   TEXT            (nullable, used only for level='series')
studyinstanceuid    TEXT            (nullable, used for level='study' and 'series')
patient_id          TEXT            (nullable, used for all levels)
label               TEXT NOT NULL
value               TEXT
level               TEXT NOT NULL DEFAULT 'series'
                    CHECK (level IN ('patient', 'study', 'series'))
created_by          TEXT NOT NULL
created_at          TIMESTAMPTZ DEFAULT now()
notes               TEXT
```

Partial unique indexes (shared annotations — one value per entity+label):

- `idx_ann_shared_series` on `(seriesinstanceuid, label) WHERE level = 'series'`
- `idx_ann_shared_study` on `(studyinstanceuid, label) WHERE level = 'study'`
- `idx_ann_shared_patient` on `(patient_id, label) WHERE level = 'patient'`

### `label_definitions`

```text
id          SERIAL PRIMARY KEY
name        TEXT NOT NULL UNIQUE
description TEXT
level       TEXT NOT NULL DEFAULT 'series'
            CHECK (level IN ('patient', 'study', 'series'))
datatype    TEXT NOT NULL DEFAULT 'bool'
            CHECK (datatype IN ('bool', 'int', 'text', 'select'))
options     TEXT
created_by  TEXT NOT NULL
created_at  TIMESTAMPTZ DEFAULT now()
```

### `users`

```text
username             TEXT PRIMARY KEY
password_hash        TEXT NOT NULL
is_admin             BOOLEAN NOT NULL DEFAULT FALSE
created_at           TIMESTAMPTZ DEFAULT now()
must_change_password BOOLEAN NOT NULL DEFAULT FALSE
password_changed_at  TIMESTAMPTZ
```

`must_change_password` is set TRUE when an admin creates the user (or runs
`manage_users.py passwd …` against them) and cleared by
`POST /api/auth/change-password` once the user picks their own credential.
While the flag is TRUE the API rejects every non-auth endpoint with
`403 password_change_required`. `password_changed_at` is stamped when the user
last self-set their password (NULL means never self-chosen).

### `user_preferences`

```text
username   TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE
level      TEXT NOT NULL CHECK (level IN ('patient', 'study', 'series', '_global'))
prefs      JSONB NOT NULL DEFAULT '{}'
updated_at TIMESTAMPTZ DEFAULT now()
PRIMARY KEY (username, level)
```

### `annotations_history`

```text
history_id       BIGSERIAL PRIMARY KEY
operation        CHAR(1) NOT NULL           -- I | U | D
operation_at     TIMESTAMPTZ DEFAULT now()
operation_by     TEXT DEFAULT 'system'
annotation_id    INTEGER NOT NULL
level            TEXT NOT NULL
entity_id        TEXT NOT NULL
label            TEXT NOT NULL
value_before     TEXT                       -- NULL on INSERT
value_after      TEXT                       -- NULL on DELETE
notes_before     TEXT                       -- NULL on INSERT
notes_after      TEXT                       -- NULL on DELETE
created_by       TEXT
```

Indexes: `annotations_history_annotation_id_idx` `(annotation_id, operation_at DESC)`, `annotations_history_entity_id_idx` `(entity_id, operation_at DESC)`.

Populated by `annotations_audit_trg` trigger (PL/pgSQL). See [`../operations/annotation_history.md`](../operations/annotation_history.md).

---

## How the web app queries the DB

- **Patients**: listed from the `patient` registry, LEFT JOINing `lvo_clinical_data` on `c.study_id = p.patient_id` to display `COALESCE(c.stroke_date, p.stroke_date::date::text)` — the clinical date when matched, the imaging-derived date otherwise.
- **Studies**: listed from `image_study`, and modality is aggregated from `image_series` by `studyinstanceuid`.
- **Series**: listed from `image_series` and LEFT JOINs `image_study` to include `study_type`.
- **Annotations** are joined/attached per row and **inherit downward** (patient → study → series) in API responses.

Connection settings are read from `.env`:
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

---

## Related documentation

- Operator procedures for cold storage: [`../cold_storage/runbook.md`](../cold_storage/runbook.md)
- Design rationale: [`../cold_storage/design.md`](../cold_storage/design.md)
