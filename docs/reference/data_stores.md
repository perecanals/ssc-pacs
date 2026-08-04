# Data stores (PostgreSQL)

**Purpose:** Authoritative reference for logical databases, source tables, web-app-owned tables, and optional cold-storage schema. For query/join behavior at a glance, see also [How the web app queries the DB](#how-the-web-app-queries-the-db). For the dual-DB rationale see [`architecture.md`](architecture.md) §3.

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
    not a clinical row exists in `clinical_data`.
  - fields: `patient_id` (PK), `stroke_date` (imaging-derived =
    `MIN(image_study.acquisitiondatetime)`), `import_id`/`import_label` (origin
    batch, preserved on conflict), `dataset` (`text[]`, union-accumulated),
    `created_at`, `updated_at`
  - **Imaging-derived only — do not add clinical columns here.** Alembic `0017`
    added a `femoral_sheath_time` column and `0018` removed it again: a column
    per clinical variable means a migration on an upstream-owned table, a
    COALESCE expression, a frontend column, and a mirror into the out-of-band
    `create_patient.sql`, every time. Clinical variables belong in `annotations`
    as patient-level labels — see
    [`../operations/commands.md`](../operations/commands.md)
    (`scripts/admin/bulk_set_label_values.py`).
- **`clinical_data`** (clinical side-table — *not* the patient spine; renamed
  from `lvo_clinical_data` in revision `0020`)
  - **Optional.** A clinical import a deployment may not have at all. Every
    read is guarded (`common.table_exists` in the web app,
    `inspect(...).has_table` in ingestion). Without it the patient tab shows the
    imaging-derived `stroke_date` for everyone and the timepoint classifier
    anchors on each episode's own thrombectomy study — see below.
  - clinical variables (demographics, outcomes, etc.). Retired as a roster: the
    patient tab joins it only to prefer its `stroke_date` when a patient is
    clinically matched.
  - **Scoped exception (Alembic `0015`)**: the timepoint classifier reads three
    time columns — `femoral_sheath_time`, `receiving_arrival_time`,
    `time_recognized` — to anchor `image_study.timepoint` on the thrombectomy
    puncture. That is the *only* other sanctioned read; do not widen it.
  - key fields: `study_id` (the patient id; joined as `c.study_id = patient.patient_id`), `stroke_date` (TEXT)
  - Contains identifiable clinical data. Treat as sensitive: query it in the
    aggregate, and don't page through row values without a reason.
- **`image_study`** (study-level imaging metadata)
  - typical fields: `patient_id`, `studyinstanceuid`, `studydescription`, `study_type`, `study_path`, `acquisitiondatetime`, `import_id`, `import_label`
  - storage-size rollups (Alembic `0012`, `double precision`, decimal MB): `compressed_size_mb`, `decompressed_size_mb` — stay NULL until every child series has that size
  - classification: **`study_type`** — machine-derived from `StudyDescription` at ingest, plus `study_type_version` (Alembic `0015`). See [`image_ingestion_protocol.md`](image_ingestion_protocol.md) §How `series_type` and `study_type` are detected
  - temporal (Alembic `0015`, extended `0016`): **`timepoint`** (`BL` / `THROMBECTOMY` / `FU` / NULL), `timepoint_anchor_source`, `hours_to_event` (signed), `timepoint_version`, **`episode`** (1-based, `0016`). Anchored **per episode** on the **femoral-sheath puncture** from `clinical_data` — *not* stroke onset, so `BL` means pre-thrombectomy — falling back to the episode's own `THROMBECTOMY` study when there is no clinical anchor (`timepoint_anchor_source = 'thrombectomy_study'`, covers non-LVO patients + the second episode of the `11-*` multi-episode cohort). Only 59% of clinical rows carry a recorded puncture; the rest are `+5h`/`+10h` estimates, which is why `timepoint_anchor_source` exists — filter on it before trusting a timepoint. `acquisitiondatetime_source` (`0016`, `acquisition` | `study`) records which DICOM clock built `acquisitiondatetime`. See [`image_ingestion_protocol.md`](image_ingestion_protocol.md) §How `timepoint` is detected
- **`image_series`** (series-level imaging metadata)
  - typical fields: `patient_id`, `studyinstanceuid`, `seriesinstanceuid`, `seriesdescription`, `modality`, `acquisitiondatetime`, `acquisitiondatetime_source` (`0016`)
  - file pointers: `dicom_dir_path`, `nifti_path`
  - optional cold storage: **`dicom_archive_path`** — path to per-series `*.tar.zst` when using `cold_path_cache` mode
  - storage sizes (Alembic `0012`, `double precision`, decimal MB): `compressed_size_mb`, `decompressed_size_mb`
  - classification: **`series_type`** — one of `NCCT` / `CTA` / `CTP` / `PWI` / `DWI` (the reference implementation's five) plus `ADC` / `MRA_TOF` / `MRA_CE`, or NULL. Everything else in that taxonomy (bone, dual-energy, topogram, test bolus, RAPID output, projections, CT reformats, DSA) is an **exclusion, not a type** — `series_type` is NULL and **`series_type_rule`** records which exclusion fired, so a NULL is a decision, not a failure. ~84% of the corpus is NULL; read the rule before concluding a series is unclassified. Plus `series_type_version` (Alembic `0015`). See [`image_ingestion_protocol.md`](image_ingestion_protocol.md) §How `series_type` and `study_type` are detected
  - preference rank (Alembic `0015`): **`series_type_rank`** (integer, 1 = the series of that type to use for this patient) and **`series_label`** = `series_type || '_' || series_type_rank`, e.g. `NCCT_1` — the value to *display* and to filter on. NULL exactly when `series_type` is NULL. A window function over the patient's other series, so it is a plain column, not GENERATED; recomputed wholesale by `scripts/admin/reclassify_series_types.py`
  - ingestion bookkeeping: `import_id`, `import_label`
  - geometry-derived: `imageshape`, **`number_of_slices`**, `slicethickness`, `scanaxialcoverage_mm`

Notes:

- The ingestion pipeline (`image_ingestion_protocols/`) upserts into `image_study` and `image_series`.
- `number_of_slices` is populated during ingest and can be backfilled for existing rows.
- **`image_series.series_type` / `image_study.study_type` are machine-owned** and are a *different axis* from the human annotation labels that happen to share those names (mirrored as `label_series_type_*` / `label_study_type_*`, sourced from `annotations`). Neither may be derived from the other, in either direction — a reclassify run must never overwrite a rater's judgement, and a rater's judgement must never be fed back into the rules.
- The web app **surfaces `series_type` (with its rank) and `timepoint` read-only**, as the `Auto Series Type` / `Auto Timepoint` columns — muted outlined pills, deliberately distinct from the editable pills of the same-named human labels beside them. It selects and filters these columns but **never writes them**; the only writers are ingestion and `reclassify_series_types.py`. See [`web_app_frontend.md`](web_app_frontend.md) §The "Auto" columns.

### web-app-owned tables (app-managed)

These tables are created and evolved by **Alembic migrations** under
`alembic/versions/`. `init_db()` runs `alembic upgrade head` on
startup. See [`../operations/schema_migrations.md`](../operations/schema_migrations.md)
for the workflow when adding a new revision.

- **`users`**: the single source of truth for end-user authentication (bcrypt password hashes). The `is_admin` column gates `/api/admin/*` endpoints and the "Orthanc Explorer" Landing card; admins are also mirrored into `orthanc_users.json` by `scripts/admin/manage_users.py` so they can reach `:8042` directly.
- **`annotations`**: multi-level (patient / study / series) annotations. Rows whose entity id no longer exists in the source tables surface as the `orphaned_annotations` category in [`../operations/reconciliation.md`](../operations/reconciliation.md).
- **`label_definitions`**: label registry (level-aware; supports bool/int/text/select).
- **`label_value_options`**: known values (controlled vocabulary) per select-type label. Indexed `(label, value)` lookup kept in sync on annotation writes and label-definition creation; the live source feeding the inline-edit dropdown and the column filter (replaces a `SELECT DISTINCT` scan of `annotations`). Global (not dataset-scoped); values persist once created.
- **`user_preferences`**: per-user persisted table layout/state and Navigator session state (JSONB).
- **`series_dicom_tags`** (Alembic `0015`): one row per series holding the full DICOM tag set of a representative instance as `jsonb` (keyed by pydicom *keyword*; private tags under a `_private` sub-key), plus the cross-instance aggregates no single header carries — `same_position_count` (the CTP/PWI/DWI discriminator, previously computed at ingest and discarded), `n_positions`, `n_instances_scanned`, `distinct_kernels`, `distinct_image_types`. GIN-indexed on `tags`, so `tags ? 'ConvolutionKernel'` / `tags @> '{...}'` are cheap. This is what makes classification *iterable*: re-deriving `series_type` for the whole corpus becomes a table scan (seconds) instead of a re-read of every cold archive (~45 min). Written by ingestion in the same transaction as `image_series`; backfilled by `maintenance/scripts/backfill_series_dicom_tags.py`. Keyed `seriesinstanceuid text PRIMARY KEY` with **no FK** to `image_series` — same pattern as `series_cache_state`, because `image_series` is upstream-owned (`alembic/env.py:UPSTREAM_TABLES`).
- **`patient_labelled` / `image_study_labelled` / `image_series_labelled`**: per-level mirror tables that join each source table with its level's annotations pivoted into label columns. Maintained (eventually consistent) by `web-app/labelled_table_sync.py` — refreshed in the background after each annotation write and once per batch at the end of an ingestion run. Used for labelled-pivot / export views; the live table read path never depends on them. (These replaced the former `snapshot_*` tables, dropped by Alembic `0013_drop_snapshot_tables`.)

Audit:

- **`annotations_history`**: append-only audit trail for every annotation change (trigger-captured). See [`../operations/annotation_history.md`](../operations/annotation_history.md).

Cold storage / hot cache (when enabled):

- **`series_cache_state`**: per-series warm status and paths for the hot cache. The series is the single source of truth; study/patient warm status is a derived aggregate. Series listings can `LEFT JOIN series_cache_state` to surface warm status. (The former per-study `cache_state` and the dead `orthanc_resource_map` tables were removed by Alembic `0010_series_cache_state`.)

### `image_series.dicom_archive_path`

Nullable `TEXT`. Populated by the offline archiver (`scripts/cold_storage/archive_all_series.py`) when series are packed to `*.tar.zst`. Used when `[storage].mode = "cold_path_cache"` in `config.toml`.

### `series_cache_state`

```text
seriesinstanceuid  TEXT PRIMARY KEY
status             TEXT NOT NULL DEFAULT 'cold'
                   CHECK (status IN ('cold', 'warming', 'hot', 'error', 'queued'))
cache_path         TEXT            -- the series' dicom_dir_path when warm
warmed_at          TIMESTAMPTZ
last_accessed_at   TIMESTAMPTZ
warming_started_at TIMESTAMPTZ
error_message      TEXT
```

The series is the cache-state unit; study/patient status is derived by
aggregating these rows (a study is `hot` only when all its series are `hot`).
Replaced the former per-study `cache_state` table (Alembic
`0010_series_cache_state`), which also dropped the dead `orthanc_resource_map`
table.

---

## Web App table DDL (logical reference)

The Alembic revisions under `alembic/versions/` are authoritative (app startup only runs `alembic upgrade head`); this section mirrors the intended shape for documentation readers.

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
instrument  TEXT                       -- free-text grouping (NULL = "Unassigned"); Alembic 0004
created_by  TEXT NOT NULL              -- the label's OWNER (see edit_policy)
created_at  TIMESTAMPTZ DEFAULT now()
edit_policy TEXT NOT NULL DEFAULT 'everyone'   -- Alembic 0019
            CHECK (edit_policy IN ('everyone', 'nobody', 'users'))
edit_users  TEXT[] NOT NULL DEFAULT '{}'       -- Alembic 0019
```

`instrument` groups labels in the sidebar and default column order (instruments
alphabetical, unassigned last). `options` holds the **curated** select values as a JSON array (set at creation;
not editable afterward). For select labels the *effective* option list returned
by `GET /api/label-definitions` is `options` ∪ the live values in
`label_value_options` — so values created inline while annotating appear in both
the inline dropdown and the column filter without editing the definition.

**Edit permissions** (`edit_policy` + `edit_users`, Alembic `0019`) answer *who
may write this label's values*:

| `edit_policy` | `edit_users` | who may set/clear values |
|---|---|---|
| `everyone` (default) | `{}` | any authenticated user — the pre-`0019` behavior |
| `nobody` | `{}` | no one via the API/UI, **admins included** |
| `users` | `{a,b,…}` | exactly those usernames ("editable by me" is `{me}`) |

- **No admin bypass.** `nobody` means nobody: the point is to stop a stray click
  silently overwriting bulk-loaded clinical data. To correct a locked value an
  admin changes the policy, edits, and changes it back — deliberate, and every
  step lands in `annotations_history`.
- Enforced server-side on `POST /api/annotations` and `DELETE
  /api/annotations/{id}` (`common.can_edit_label`); the read-only rendering in
  the UI is cosmetic. A label with **no definition row** stays editable — this
  restricts, it never newly forbids.
- Changing the policy requires being the label's **owner** (`created_by`) or an
  admin (`common.can_change_label_policy`); being listed in `edit_users` grants
  value edits, not control. Bulk-created labels have a `bulk:<user>` owner that
  matches no login, so they are admin-only to unlock — for free.
- Set from the Label Access admin page (`/admin/labels`), the label modal
  (everyone / only me / no one), or `bulk_set_label_values.py --edit-policy` at
  creation. `edit_users` is not FK'd to `users`: a deleted user leaves a stale
  name that simply never matches.
- `scripts/admin/bulk_set_label_values.py` writes direct SQL and **bypasses this
  gate by design** — it is the admin backdoor, authorized by shell + `.env`
  access.

### `label_value_options`

```text
label       TEXT NOT NULL
value       TEXT NOT NULL
created_by  TEXT
created_at  TIMESTAMPTZ DEFAULT now()
PRIMARY KEY (label, value)
```

The controlled vocabulary for select-type labels. Seeded from
`label_definitions.options` and from values already in `annotations` (Alembic
revision `0009_label_value_options` backfills both), then kept current by
`POST /api/annotations` and `POST /api/label-definitions`, which upsert select
values here in the same transaction (`record_label_value` in `common.py`).
`GET /api/labels/{name}/values` reads it directly — a fast indexed lookup that
replaced a `SELECT DISTINCT value FROM annotations` scan. Global vocabulary (not
dataset-scoped); values persist once created (pruning is a manual admin action).

### `users`

```text
username             TEXT PRIMARY KEY
password_hash        TEXT NOT NULL
is_admin             BOOLEAN NOT NULL DEFAULT FALSE
created_at           TIMESTAMPTZ DEFAULT now()
must_change_password BOOLEAN NOT NULL DEFAULT FALSE
password_changed_at  TIMESTAMPTZ
allowed_datasets     TEXT[] NOT NULL DEFAULT '{}'
```

`must_change_password` is set TRUE when an admin creates the user (or runs
`manage_users.py passwd …` against them) and cleared by
`POST /api/auth/change-password` once the user picks their own credential.
While the flag is TRUE the API rejects every non-auth endpoint with
`403 password_change_required`. `password_changed_at` is stamped when the user
last self-set their password (NULL means never self-chosen).

`allowed_datasets` holds the user's dataset grants — the `patient.dataset`
cohort tags they may see (deny-by-default: empty = no patient data; admins
bypass). Managed via the `/admin` page or `manage_users.py set-datasets`;
enforced by every patient-data endpoint and the DICOMweb proxy (see
[`architecture.md`](architecture.md) §5.4).

### `user_preferences`

```text
username   TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE
level      TEXT NOT NULL CHECK (level IN ('patient', 'study', 'series', '_global'))
prefs      JSONB NOT NULL DEFAULT '{}'
updated_at TIMESTAMPTZ DEFAULT now()
PRIMARY KEY (username, level)
```

The `patient` / `study` / `series` rows hold per-level table preferences
(column visibility, order, sort, column filters, frozen-column state). The
`_global` row holds the Navigator session state as
`{"session": {"level": ..., "filters": {...}}}` — the last-used hierarchy
level and sidebar quick filters, restored on the user's next visit.

### `annotations_history`

```text
history_id       BIGSERIAL PRIMARY KEY
operation        CHAR(1) NOT NULL           -- I | U | D
operation_at     TIMESTAMPTZ DEFAULT now()
operation_by     TEXT NOT NULL DEFAULT 'system'
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

- **Patients**: listed from the `patient` registry. When `clinical_data` exists it is LEFT JOINed on `c.study_id = p.patient_id` to display `COALESCE(c.stroke_date, p.stroke_date::date::text)` — the clinical date when matched, the imaging-derived date otherwise. When it does not (`common.table_exists` is false), the join is dropped and the expression is just `p.stroke_date::date::text`. Filter, sort, and SELECT all reuse the one expression, so the two branches cannot drift.
- **Studies**: listed from `image_study`, and modality is aggregated from `image_series` by `studyinstanceuid`.
- **Series**: listed from `image_series` and LEFT JOINs `image_study` to include `study_type`.
- **Annotations** are joined/attached per row and **inherit downward** (patient → study → series) in API responses.

Connection settings are read from `.env`:
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

---

## Related documentation

- Operator procedures for cold storage: [`../cold_storage/runbook.md`](../cold_storage/runbook.md)
- Design rationale: [`../cold_storage/design.md`](../cold_storage/design.md)
