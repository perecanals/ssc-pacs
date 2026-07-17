# Stanford Stroke Center PACS Architecture

**Purpose:** High-level deployed architecture — topology, service roles, data flows, auth, and portability. For database detail see [`data_stores.md`](data_stores.md). For packaging and config files see [`runtime_and_config.md`](runtime_and_config.md).

This document explains the deployed architecture of the PACS stack and which
parts are reusable versus specific to the current Stanford Stroke Center (SSC)
database.

---

## 1. Topology

The repo deploys one Docker service (Orthanc) and one native host service
(the web app), and relies on one existing host PostgreSQL server plus an
existing DICOM filesystem. Users reach the ports through an SSH tunnel (no
reverse proxy is bundled).

The full topology diagram, component list, and user-facing URL table are the
canonical [`system_overview.md`](system_overview.md) §2–3; this document covers
the *why* behind the deployed shape.

---

## 2. Service roles

**Orthanc** (container `ssc-orthanc`) is the PACS viewer/indexer layer: it
indexes the read-only DICOM tree, keeps an internal PostgreSQL index, serves
Orthanc Explorer 2 / OHIF / DICOMweb / the REST API, and stores study-level
labels for Orthanc Explorer 2. It does **not** own the source DICOM files,
generate the upstream `image_series`/`image_study` metadata, or store web-app
annotations.

**Web App** is a FastAPI service (native host process) serving a React frontend
and a REST API for the multi-level annotation workflow Orthanc Explorer 2 does
not support. It browses patients (from the `patient` registry, LEFT JOINing
`clinical_data` only for the clinical `stroke_date`), studies, and series;
stores shared multi-level annotations and level-aware label definitions; does
cross-level label filtering and downward annotation inheritance; authenticates
against `users`; and builds study/series-aware OHIF links for the embedded
preview pane. It does **not** index DICOMs, own the PACS metadata index, or
replace Orthanc Explorer 2.

---

## 3. Dual-database model

The most important architectural feature is the split between two logical
PostgreSQL databases.

### 3.1 Orthanc index DB

Orthanc uses its own database, typically `orthanc_db`, for internal tables
managed by the Orthanc PostgreSQL plugin.

Key properties:

- operational infrastructure for Orthanc itself
- populated by Orthanc's Folder Indexer and plugin logic
- used for PACS metadata lookup and web UI behavior
- configured through `ORTHANC__POSTGRESQL__*` environment variables
- run with `ENABLE_INDEX=true` and `ENABLE_STORAGE=false`

This means:

- Orthanc indexes metadata in PostgreSQL
- Orthanc does not duplicate the DICOM files into PostgreSQL
- the canonical image payload stays on disk

### 3.2 Research / app DB

The second logical database is the research/application database, currently
`stanford-stroke`.

It contains:

- the existing read-only source tables `patient`, `clinical_data`,
  `image_series`, and `image_study`
- web-app-owned tables:
  - `annotations` — multi-level (patient/study/series) with shared partial
    unique indexes per level (one value per entity+label; `created_by`
    tracks who last edited)
  - `label_definitions` — level-aware label registry supporting bool, int,
    text, and select datatypes
  - `label_value_options` — known values (controlled vocabulary) per
    select-type label; fast indexed lookup kept in sync on annotation writes
    (replaces a `SELECT DISTINCT` scan of `annotations`)
  - `users`
  - `user_preferences` — per-user JSONB table display preferences (column
    visibility, order, sort, filters, frozen state) keyed by username and
    level
  - `patient_labelled` / `image_study_labelled` / `image_series_labelled` —
    per-level mirror tables (source rows + annotations pivoted to label
    columns) maintained by `labelled_table_sync.py` for labelled-pivot / export
    views (eventually consistent; the live read path never depends on them)

Optional cold-storage support adds columns and tables documented in
[`data_stores.md`](data_stores.md).

This database is where the web app app gets its patient, study, and series
listings and where it stores user-generated annotations and label definitions.

### 3.3 Why the split exists

The two-database design keeps responsibilities clean:

- Orthanc's operational index stays isolated from research metadata tables
- the web app can evolve its own schema without touching Orthanc internals
- the DICOM tree can remain external and read-only
- the same host PostgreSQL server can support both layers without mixing roles

---

## 4. Data flow

### 4.1 Imaging data

1. DICOM files exist on the host filesystem at `[storage].dicom_data_root`
   (from `config.toml`).
2. Docker bind-mounts that tree read-only into the Orthanc container as
   `/dicom-data` (source resolved by `scripts/orthanc/dc.sh`).
3. Orthanc's Folder Indexer registers instances into its internal PostgreSQL
   metadata index. In production the index is populated on demand per case
   (`POST /indexer/scan`), not by a continuous whole-tree scan (`Folders: []`).
4. OHIF and Orthanc Explorer 2 read through Orthanc, not directly from the
   research database.

The DICOM Application Entity Title (AE Title) is configured as `SSC`.

**Optional cold storage mode** (`mode = "cold_path_cache"` under `[storage]` in the stack-root `config.toml`):
canonical series payloads live as `*.tar.zst` under `[storage].cold_archive_root`.
On warm, the web app extracts an archive back to the **original** `dicom_dir_path` recorded in
`image_series`; on evict it deletes those extracted files. This requires the custom
`ssc-orthanc:patched-indexer` image with `"RemoveMissingFiles": false`, so Orthanc's index keeps
pointing at the original paths even while the files are absent — no re-ingestion is needed.
Legacy loose files under `[storage].dicom_data_root` are read directly when `mode = "legacy"`.
(An earlier `cold_cache` design that warmed into a separate hot-cache dir and re-POSTed
DICOMs to Orthanc was removed — see [`../cold_storage/design.md`](../cold_storage/design.md).)

- Design rationale and benchmarks: [`../cold_storage/design.md`](../cold_storage/design.md)
- Operator steps and component map: [`../cold_storage/runbook.md`](../cold_storage/runbook.md)

### 4.2 Metadata and annotations

1. `patient`, `image_series`, and `image_study` provide the metadata that drives
   the web app (patient browsing from the `patient` spine, studies from
   `image_study`, series from `image_series`). `clinical_data` is **retired
   as a roster**: it is never the patient source and is otherwise unqueried —
   the patient list joins it in a single LEFT JOIN (`routes/studies.py`) only to
   prefer its clinical `stroke_date` via `COALESCE`, and only when the table
   exists (it is optional; see §"Redeploying elsewhere").
2. The web app reads these tables to build patient, study, and series browsers
   with filtering, sorting, and pagination. Series listings JOIN `image_study`
   for `study_type`.
3. Web App writes shared annotations at three levels (patient, study,
   series) and level-aware label definitions back into the same research/app
   database. Annotations are global: any user can edit any annotation, and
   the value is shared across all users (`created_by` tracks the last
   editor).
4. Annotations inherit downward: parent-level annotations are attached to child
   rows as `inherited_annotations`. Cross-level filtering allows filtering any
   level by annotations at a different level.
5. When a study or series row is selected in the web app, the backend builds
   an OHIF viewer URL and the frontend can load it inside an embedded preview
   pane. Study selections load the study viewer; series selections use a
   series-specific OHIF URL scoped to that study.
6. Orthanc study labels are stored inside Orthanc and manipulated via Orthanc's
   UI or REST API, not through the web app tables.

---

## 5. Authentication model

End-user authentication is single-sourced from the PostgreSQL `users` table.
The web app is the only login point for end users; it reverse-proxies
OHIF and DICOMweb to Orthanc on the user's behalf.

### 5.1 Web App auth (end users)

The web app authenticates against `users`:

- passwords are bcrypt hashes
- login returns a JWT cookie
- authenticated writes use the JWT identity as `created_by`
- `/ohif/*` and `/dicom-web/*` are reverse-proxied to Orthanc by the web app
  (see `web-app/routes/proxy.py`), attaching the service-account credential
  from `.env`. End users never present credentials to Orthanc directly.

#### OHIF asset caching

Orthanc serves the OHIF build with no `Cache-Control`, `ETag`, or
`Last-Modified`, so without intervention every viewer open re-downloads the
whole bundle (~21 MiB across ~54 requests) — the black screen before OHIF
appears. The proxy therefore stamps
`Cache-Control: private, max-age=31536000, immutable` onto **content-hashed**
assets only (`is_immutable_ohif_asset()` in `routes/proxy.py`: a 20-hex webpack
contenthash as a whole dot-delimited segment, e.g. `app.bundle.<hash>.js`,
`<hash>.woff2`, `<hash>.wasm`). A rebuild changes the bytes, hence the hash,
hence the URL — so a cached entry can never go stale.

Unhashed siblings (`app.bundle.css`, `app-config.js`, `manifest.json`, and the
`/ohif/` + `/ohif/viewer` entry documents) are deliberately **not** cached: they
keep their names across rebuilds, and Orthanc sends no validator for a
revalidating policy to use. This is what makes deployment config changes take
effect on the next load. `sliding_jwt` (`app.py`) skips the cacheable assets for
the same reason it skips `/assets/` — a `Set-Cookie` on them is pure waste. The
session still slides on `/dicom-web/*` and `/api/*`, which run throughout a
viewing session.

This addresses repeat opens only. A *first* load still transfers the full
~21 MiB: Orthanc ignores `Accept-Encoding`, so nothing is compressed.

### 5.2 Orthanc auth (service account + admins only)

`orthanc_users.json` is no longer a runtime user store. It contains:

- the **service account** (matching `ORTHANC_ADMIN_USER` / `ORTHANC_ADMIN_PASSWORD`
  in `.env`), used by the web app to proxy requests on behalf of any logged-in
  user, and by host-local maintenance scripts
- each **admin user** (`users.is_admin = TRUE`), so admins can log in to
  `:8042/ui/app/` (Orthanc Explorer 2) and `:8042/ohif/` as themselves with
  per-user attribution in Orthanc's own logs

The file is owned by `scripts/admin/manage_users.py`; do not edit by hand.

### 5.3 Shared provisioning

`scripts/admin/manage_users.py` is the canonical tool for user provisioning:

- `add` / `passwd` / `remove` always touch the `users` table; they also update
  `orthanc_users.json` when `is_admin=True`
- `add --datasets <csv>` / `set-datasets` manage per-user dataset grants
  (see 5.4)

Credential rotation lives in dedicated siblings:

- `scripts/admin/rotate_service_account.py rotate` rewrites
  `ORTHANC_ADMIN_PASSWORD` in `.env` and the matching entry in
  `orthanc_users.json` atomically (does not touch the DB); `check` verifies the
  two agree.
- `scripts/admin/rotate_db_password.py rotate` runs `ALTER ROLE` on the live DB
  and rewrites `DB_PASSWORD` in `.env`; `check` verifies `.env` authenticates.

### 5.4 Dataset-level authorization (per-user cohort access)

Beyond authentication, every non-admin user carries a **dataset scope**:
`users.allowed_datasets text[]`, a subset of the cohort tags found in
`patient.dataset` (e.g. `PRECISE`, `CRISP2/LVO`).

- **Deny by default** — an empty grant set (the default for new users) means
  the user sees *no* patient data until an admin grants datasets.
- **Admins bypass** — `is_admin = TRUE` ignores the scope entirely.
- **Enforced server-side on every endpoint** that returns or mutates
  patient-derived data:
  - the list endpoints (`/api/patients`, `/api/studies`, `/api/series`, plus
    sidebar option endpoints) filter rows to patients whose `dataset`
    overlaps the scope (`dataset && allowed`);
  - detail endpoints keyed by a patient/study/series id (sub-row listings,
    `/api/ohif-link`, warm/evict/cache-status, annotation reads/writes)
    return **404** for out-of-scope ids, so they are indistinguishable from
    nonexistent ones;
  - the DICOMweb reverse proxy resolves each `/dicom-web/*` request to a
    scoped entity — the StudyInstanceUID (path or QIDO query param), or,
    failing that, the PatientID (`00100020`/`PatientID` query param; OHIF's
    study-browser panel searches by PatientID) — and rejects out-of-scope
    requests with 403. Lookups are served from in-process TTL caches
    (`web-app/dataset_access.py`: user scope 30 s, study/patient→datasets
    5 min), so per-frame WADO requests cost no DB round-trips. QIDO searches
    with neither identifier are denied for non-admins.
  - The proxy also strips `Modality` (0008,0060) from `includefield` on
    study-level QIDO searches (`routes/proxy.py:sanitize_study_search_query`):
    it is a series-level tag, so Orthanc answers it by opening one stored
    DICOM file per matching study — a 500 for the whole search when any file
    is evicted (cold storage) or stale. Lossless: Orthanc always returns the
    index-computed ModalitiesInStudy (0008,0061), which OHIF falls back to.
- **Managed by**: the `/admin` page (admin-only, users × dataset checkboxes,
  `GET /api/admin/users` + `PUT /api/admin/users/{username}/datasets`) or
  `scripts/admin/manage_users.py set-datasets`.

Known limitation: `/api/labels` and `/api/labels/summary` expose label names
and aggregate counts across all data (no identifiers or values).
`/api/labels/{name}/values` returns a select label's controlled vocabulary
from the `label_value_options` table — a **global** value set, not scoped per
dataset (only the value strings are shared, never patient data).

### 5.5 Label-level authorization (who may edit a label's values)

The sibling of 5.4: that gates which cohorts a user may **see**, this gates
which labels a user may **write**. Each label carries
`label_definitions.edit_policy` (`everyone` / `nobody` / `users`) plus
`edit_users text[]` (Alembic `0019`). Schema and the full table:
[`data_stores.md`](data_stores.md#label_definitions).

- **Allow by default** — `everyone`, so nothing changed on upgrade. Restricting
  is an explicit act. (Contrast 5.4, which is deny-by-default: that one guards
  patient data, this one guards *data integrity*.)
- **Admins do NOT bypass** — the deliberate opposite of 5.4. `nobody` means
  nobody: the threat is a stray click overwriting bulk-loaded clinical data, and
  the person most likely to be clicking with admin rights is the one who loaded
  it. Correcting a locked value means changing the policy first — deliberate,
  and audited in `annotations_history`.
- **Enforced server-side** on `POST /api/annotations` (which covers both the
  upsert and the select-vocabulary extension) and `DELETE /api/annotations/{id}`
  — clearing is a write. `common.can_edit_label`, read inline per request; no
  cache, unlike 5.4's proxy hot path. The UI's read-only rendering is cosmetic.
- **Ownership**: `created_by` owns the label; only the owner or an admin may
  change its policy (`common.can_change_label_policy`). Being listed in
  `edit_users` confers value edits, not control. Bulk-created labels are owned
  by `bulk:<user>`, which matches no login — so they are admin-only to unlock
  without any special-casing.
- **Managed by**: the `/admin/labels` page (admin-only, policy + user
  checkboxes, `GET /api/admin/label-definitions` +
  `PUT /api/admin/label-definitions/{id}/permissions`), the label modal
  (everyone / only me / no one, for the owner), or
  `bulk_set_label_values.py --edit-policy` at creation.

Known limitation: the CLI writes direct SQL and bypasses this gate by design —
it is the admin backdoor, authorized by shell + `.env` access. The
`annotations_history` trigger records those writes but does not gate them.

---

## 6. Packaging model

Packaging facts (compose wiring, service units, frontend build) are canonical in
[`runtime_and_config.md`](runtime_and_config.md). Two points that matter
architecturally:

- **Orthanc** runs the locally-built custom image `ssc-orthanc:patched-indexer`
  (a digest-pinned base plus the patched Folder Indexer — see
  [`../../orthanc-indexer-patched/Dockerfile`](../../orthanc-indexer-patched/README.md)),
  **not** an upstream registry image. The repo provides `docker-compose.yml`
  (Orthanc only), `orthanc.json`, and the tool-managed `orthanc_users.json`.
- **Web App** runs natively (no container): uvicorn under systemd
  (`ssc-web-app.service`) on Linux — the reference deployment — or under
  launchd (`com.ssc.webapp`) on macOS, serving the pre-built `web-app/dist/`.
  Node is build-time only.

---

## 7. Portable versus deployment-specific parts

### 7.1 Portable core

These parts are broadly reusable on another server if the target deployment will
follow the same pattern:

- `docker-compose.yml` (Orthanc only)
- `orthanc.json`
- `web-app/` (FastAPI backend + React frontend)
- the service-unit templates (`deploy/systemd/*.in`, `deploy/launchd/*.plist.in`) rendered by
  the two installers
- `scripts/admin/manage_users.py`
- `init_orthanc_db.sh`

### 7.2 Deployment-specific parts

These parts depend on each deployment's data sources rather than on the stack
itself.

`image_ingestion_protocols/` is the general pipeline that creates and curates
`image_series` and `image_study`; what varies per deployment is its input:

- the source DICOM directory layout it walks (configured per run via the YAML)
- the optional clinical enrichment: it reads the `clinical_data` table when
  present and skips that step entirely when a deployment has no clinical source
- it is only needed when ingesting new imaging data, not to deploy the PACS
  services themselves

### 7.3 Practical guidance for new deployments

If a new server already has:

- a DICOM tree
- a PostgreSQL server
- metadata tables equivalent to `patient`, `image_series`, `image_study`, and
  (optionally) `clinical_data`

then the PACS stack can usually be redeployed without using
`image_ingestion_protocols/`. The patient tab is sourced from the `patient`
registry; `clinical_data` is an optional clinical side-table — if absent,
the patient tab still works and shows the imaging-derived `stroke_date` instead
of the clinical one, and the timepoint classifier anchors each episode on its
own thrombectomy study. Every read of the table is guarded by an existence probe
(`common.table_exists` in the web app, `inspect(...).has_table` in ingestion), so
"absent" is a supported deployment shape, not a crash.

If the new deployment does not already have equivalent metadata tables, that
metadata-ingestion problem must be solved separately from the PACS deployment.

---

## 8. Operational caveats

Current repo behavior that matters architecturally:

- `scripts/orthanc/check_status.sh` reads Orthanc credentials from the stack-root `.env` (`ORTHANC_ADMIN_USER` / `ORTHANC_ADMIN_PASSWORD`)
- all Orthanc-facing helper scripts use `ORTHANC_ADMIN_USER` /
  `ORTHANC_ADMIN_PASSWORD` from `.env`
- **`scripts/admin/teardown.sh` is destructive** — it stops the stack, removes
  Orthanc volumes, drops the Orthanc DB/role, and edits `.env` (it does not stop
  the native web-app service). It reads `ENV_FILE="$STACK_DIR/.env"` — the same
  stack-root `.env` (`stanford-stroke-pacs/.env`) everything else uses (an
  earlier "two levels above the repo root" behavior no longer applies). The
  destructiveness is confirmation-guarded; use with care.

These are documentation-relevant caveats, not fundamental design choices.
