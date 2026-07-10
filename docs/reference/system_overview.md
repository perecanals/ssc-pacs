# System overview

**Purpose:** One place to see the whole PACS stack — the custom SSC research
database, Orthanc, OHIF, the web app, and the cold-storage layer — and how
they fit together. Deliberately overview-level; link out for detail.

- Narrative on service roles, portability: [`architecture.md`](architecture.md)
- Runtime / packaging / ports: [`runtime_and_config.md`](runtime_and_config.md)
- Database tables and columns: [`data_stores.md`](data_stores.md)
- Cold storage design and rationale: [`../cold_storage/design.md`](../cold_storage/design.md)
- Cold storage operations: [`../cold_storage/runbook.md`](../cold_storage/runbook.md)
- Ingest pipeline: [`image_ingestion_protocol.md`](image_ingestion_protocol.md)

---

## 1. Mental model in one paragraph

A single host runs one PostgreSQL server (two logical DBs), one Docker
container (Orthanc with a custom Folder Indexer), and one native service
(the web app, FastAPI + React). The DICOM payload lives on the host
filesystem — either as loose files (legacy mode) or as per-series
`*.tar.zst` archives that are extracted on demand (`cold_path_cache` mode,
current production). Orthanc serves OHIF and Orthanc Explorer 2 over its
index of that filesystem. The Web App is a research UI that reads
upstream metadata tables, writes multi-level annotations, and embeds OHIF
for row-by-row image review. Users reach both services through an SSH
tunnel.

---

## 2. Topology

```
            ┌─────────────────────────────────────────────────────────────────┐
            │                          Single host                            │
            │         (Linux systemd — reference; macOS launchd too)          │
            │                                                                 │
 Browser ───┼──► :8043  web app  (FastAPI + React, native service)          │
 (via SSH   │         │                                                       │
  tunnel)   │         │  ┌──────────── service-to-service ─────────────┐      │
            │         ▼  ▼                                              │     │
            │  ┌─────────────────┐                                      │     │
            │  │ stanford-stroke │◄──── reads metadata ────────┐        │     │
            │  │ (PostgreSQL)    │                             │        │     │
            │  │                 │◄──── writes annotations ────┘        │     │
            │  └─────────────────┘                                      │     │
            │                                                           ▼     │
 Browser ───┼──► :8042  ssc-orthanc container  (custom image,                 │
            │          `ssc-orthanc:patched-indexer`)                         │
            │              │                                                  │
            │              ├──► orthanc_db (PostgreSQL) — index only          │
            │              │                                                  │
            │              └──► /dicom-data :ro  (bind mount)                 │
            │                     │                                           │
            │                     ▼                                           │
            │  [storage].dicom_data_root/    ← loose DICOMs (hot)            │
            │  [storage].cold_archive_root/  ← *.tar.zst (cold)              │
            │       (paths from config.toml; see runtime_and_config.md)       │
            │                                                                 │
            └─────────────────────────────────────────────────────────────────┘
```

User-facing URLs:

| URL | Served by |
|-----|-----------|
| `http://localhost:8043/` | Web App landing page |
| `http://localhost:8043/app/` | Web App React app (annotation workflow) |
| `http://localhost:8042/ui/app/` | Orthanc Explorer 2 (native PACS browser) |
| `http://localhost:8042/ohif/` | OHIF viewer (standalone) |
| `http://localhost:8042/dicom-web/` | DICOMweb endpoint (used by OHIF) |

---

## 3. Components

### 3.1 Orthanc (`ssc-orthanc`)

Role: index of the DICOM tree, DICOMweb + REST, and host for the OHIF and
Orthanc Explorer 2 web apps.

- Runs as a Docker container in host-network mode.
- Uses a **custom image** `ssc-orthanc:patched-indexer` built locally from
  [`orthanc-indexer-patched/`](../../orthanc-indexer-patched/README.md).
- Image = a digest-pinned base (see
  [`orthanc-indexer-patched/Dockerfile`](../../orthanc-indexer-patched/README.md))
  + a patched Folder Indexer `.so` that honours `RemoveMissingFiles: false`
  (required by `cold_path_cache`).
- Storage backend is the Folder Indexer plugin itself
  (`ORTHANC__POSTGRESQL__ENABLE_STORAGE=false`). DICOM instances are never
  copied into Orthanc's own storage — they're referenced by filesystem path
  inside `orthanc_db`.
- AET `SSC`, DICOM port `4242`, HTTP `8042`.

### 3.2 OHIF

OHIF is bundled with Orthanc as the `Ohif` plugin (`/ohif` route).
It consumes DICOMweb (`/dicom-web/`) served by Orthanc. The Web App
does not embed its own OHIF build — it builds a URL to Orthanc's OHIF
(`/ohif/viewer?StudyInstanceUIDs=...&SeriesInstanceUIDs=...`) and loads
it in an iframe inside the Navigator's preview pane.

### 3.3 Web App

Role: research-oriented browsing + annotation UI that Orthanc Explorer 2
doesn't cover.

- FastAPI app (`web-app/app.py`) served by uvicorn on `:8043`.
- Serves the pre-built React frontend (`web-app/dist/`) as static files.
- Runs as a native service — the `ssc-web-app.service` systemd unit on Linux
  (reference deployment), or launchd (`com.ssc.webapp`) on macOS — in the
  `ssc-pacs` conda env.
- Reads `web-app/config.py` → stack-root `config.toml` for non-secrets
  (storage mode, paths, session length).
- Reads secrets from stack-root `.env`.
- Talks to both `stanford-stroke` (research data + own tables) and Orthanc
  (service-to-service REST for OHIF link resolution and the occasional
  lookup).

### 3.4 Custom Folder Indexer plugin

Source at [`orthanc-indexer-patched/`](../../orthanc-indexer-patched/README.md).
Upstream is the
[Orthanc Folder Indexer plugin](https://orthanc.uclouvain.be/book/plugins/indexer.html);
the fork adds the `RemoveMissingFiles` config flag. The deployed
`orthanc.json` runs with no continuous scan:

```json
"Indexer": {
  "Enable": true,
  "Folders": [],
  "ScanRoots": ["/dicom-data"],
  "Interval": 60,
  "RemoveMissingFiles": false
}
```

`Folders: []` means the plugin does **not** walk the whole tree on a timer;
new data is registered on demand per case via `POST /indexer/scan` from the
ingestion executor (see §7). When `RemoveMissingFiles: false`, the index does
**not** drop DICOM instances whose files have disappeared — keeping the index
stable during eviction/rewarm cycles, foundational for `cold_path_cache`.

---

## 4. Data stores

### 4.1 PostgreSQL — two logical databases

One PostgreSQL server hosts both. Connection params and credentials are in
`.env` (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, plus
`PG_ORTHANC_*` for the Orthanc DB).

```
┌─ orthanc_db ─────────────────────────────────────┐
│  owned by Orthanc PostgreSQL plugin              │
│  (treat as read-only; mutate only via            │
│   sanctioned Orthanc enrichment)                 │
│                                                  │
│  resources, metadata, mainDicomTags,             │
│  dicomidentifiers, attachedfiles, labels, ...    │
│                                                  │
│  → indexed via Folder Indexer scans              │
│  → queried by cold_storage/cleanup_loose_dicoms.py│
│    "is this SeriesInstanceUID indexed?"          │
└──────────────────────────────────────────────────┘

┌─ stanford-stroke ────────────────────────────────┐
│  upstream / research metadata (read-only to app):│
│  ├── patient               (patient level)       │
│  ├── lvo_clinical_data     (clinical side-table)  │
│  ├── image_study           (study level)         │
│  └── image_series          (series level)        │
│       ├── dicom_dir_path                         │
│       ├── dicom_archive_path   ← cold storage    │
│       ├── nifti_path           (legacy mode only)│
│       └── import_id, import_label                │
│                                                  │
│  web-app-owned:                                │
│  ├── users              (bcrypt + is_admin)      │
│  ├── user_preferences   (JSONB, per-level)       │
│  ├── annotations        (level='patient'|        │
│  │                       'study'|'series')       │
│  ├── annotations_history (audit trail)           │
│  ├── label_definitions  (level-aware)            │
│  ├── label_value_options (select vocabulary)     │
│  └── {patient,image_study,image_series}_labelled │
│         (per-level labelled mirror tables)       │
│                                                  │
│  Cold storage bookkeeping:                       │
│  └── series_cache_state (per-series hot/cold;    │
│                          study/patient derived)  │
└──────────────────────────────────────────────────┘
```

See [`data_stores.md`](data_stores.md) for column-by-column detail.

### 4.2 Filesystem

Both roots come from `config.toml` `[storage]` (see
[`runtime_and_config.md`](runtime_and_config.md)):

```
{dicom_data_root}/                    (transient in cold_path_cache mode,
│                                      permanent in legacy mode. Bind-mounted
│                                      read-only into the Orthanc container as
│                                      /dicom-data.)
└── {patient_id}/
    └── {studyinstanceuid}/
        └── {seriesdescription}/
            └── {seriesinstanceuid}/
                ├── DICOM/               ← dicom_dir_path
                └── NIFTI/image.nii.gz   ← legacy mode only

{cold_archive_root}/                  (canonical in cold_path_cache mode)
└── {patient_id}/
    └── {studyinstanceuid}/
        └── {seriesdescription}/
            └── {seriesinstanceuid}/
                └── DICOM.tar.zst        ← dicom_archive_path
```

The archive path deterministically mirrors the loose path — swap
`dicom_data_root` for `cold_archive_root` and replace the leaf `DICOM/`
with `DICOM.tar.zst`. That makes `resolve_series_archive()` work without
a DB lookup when `dicom_archive_path` is NULL.

Archive format: flat — DICOM files sit at the archive root, no `DICOM/`
directory wrapper.

---

## 5. Storage modes

Controlled by `config.toml` `[storage].mode`. Two values:

| | `legacy` | `cold_path_cache` (production) |
|---|---|---|
| Canonical store | loose DICOMs in `dicom_data_root` | `*.tar.zst` in `cold_archive_root` |
| Hot cache | same as canonical | `dicom_data_root` (transient; empty when everything is cold) |
| Orthanc index | populated by routine Folder Indexer scans | **permanent** — never eroded thanks to `RemoveMissingFiles: false` |
| Warming | N/A | extract archive → `dicom_dir_path` |
| Eviction | N/A | rmtree `dicom_dir_path`; index unchanged |
| NIFTI produced by ingestion protocol | yes (`NIFTI/image.nii.gz` sibling) | no — on-demand via `scripts/dicom/dicom_to_nifti.py` |
| Requires custom Orthanc image | no | yes (`ssc-orthanc:patched-indexer`) |

See [`../cold_storage/design.md`](../cold_storage/design.md) for why
`cold_path_cache` needs the patched indexer and why the previous
`cold_cache` mode was removed.

---

## 6. Request flows

### 6.1 User opens a study from the web app

```
  click row in DataTable
      │
      ▼
  Navigator.jsx → resolveOhifViewerUrl(studyUID)
      │
      ├── GET /api/ohif-link/{studyUID}           → backend
      │     ├── read study status = aggregate over its series_cache_state
      │     │   rows (hot only when ALL series hot)   (cold_path_cache mode)
      │     ├── if 'cold'  → {status:'cold'}
      │     ├── if 'warming' → {status:'warming'}
      │     ├── if 'hot' → defensive FS probe:
      │     │                 if dicom_dir_path actually on disk:
      │     │                    POST Orthanc /tools/lookup
      │     │                    → build /ohif/viewer?... URL
      │     │                    → {status:'ready', url}
      │     │                 else:
      │     │                    clear stale series_cache_state rows, treat as cold
      │     └── legacy mode: skip cache state, just lookup + build URL
      │
      ├── (if cold) POST /api/studies/{studyUID}/warm
      │     ├── route handler (async, ~ms):
      │     │     ├── cache_manager.estimate_warm_disk_space(uid)
      │     │     │     → 507 if required > available  (STOP)
      │     │     ├── loop.run_in_executor(app.state.warm_executor,
      │     │     │                         _run_warm_with_metrics, uid)
      │     │     └── return 202 {ok, queued, studyinstanceuid}
      │     └── worker thread (bounded pool, `warm_workers` from config):
      │           → cache_manager.warm_study()  (wrapper → warm_series over
      │             the study's series; study-open warms the whole study)
      │               └── for each series, sequentially:
      │                   ├── pg advisory lock on that seriesUID
      │                   ├── mark its series_cache_state.status='warming'
      │                   ├── untar_zst(archive, dicom_dir_path)
      │                   └── mark its series_cache_state.status='hot'
      │             (study status is the aggregate — hot once all series hot)
      │
      ├── poll /api/studies/{studyUID}/cache-status until 'hot'
      │
      └── GET /api/ohif-link/{studyUID} → now returns {status:'ready', url}
            │
            ▼
      iframe src = url
            │
            ▼
  OHIF (served by Orthanc) → DICOMweb → reads files from /dicom-data/...
                                        (the restored loose dir)
```

### 6.2 DICOM download (`GET /api/series/{uid}/dicom-zip`)

**Admin-only** (`Depends(require_admin)`): bulk DICOM export is a
privilege, not a public read like the browsing endpoints — non-admins get
403 (401 if unauthenticated), and the frontend hides the download button
for them. Both modes emit a `.zip` whose top-level folder is
`{patient_id}_{seriesdescription}`:

- **Legacy:** `ZipStream.from_path(dicom_dir, arcname=folder)` streams
  directly from the loose dir.
- **Cold:** extract `.tar.zst` → tempdir → `ZipStream.from_path(tempdir,
  arcname=folder)` → stream → cleanup tempdir in generator's `finally`.

macOS Archive Utility handles the resulting zip natively.

### 6.3 OHIF viewer data path

```
  OHIF iframe loads /ohif/viewer?StudyInstanceUIDs=...
        │
        ▼
  Orthanc (OHIF plugin) → /dicom-web/studies/{uid}/series/... (WADO-RS)
        │
        ▼
  Orthanc reads DICOM file at path recorded by the Folder Indexer
        │
        ▼
  Bytes flow back through DICOMweb → OHIF renders in the browser
```

---

## 7. Ingest flow (new imaging data)

`image_ingestion_protocols/` is the SSC-specific pipeline. One run per
batch:

```
  source case dir
        │
        ▼
  scan + group → case_series_table, case_study_table
        │
        ▼
  filter existing / validate against lvo_clinical_data
        │
        ▼
  copy DICOMs →  dicom_data_root/.../DICOM/
        │
        ▼
  (cold_path_cache only) compress each series →
        cold_archive_root/.../DICOM.tar.zst
        │
        ▼
  (legacy only) create NIFTI sibling
        │
        ▼
  upsert image_study + image_series (+ patient)
        │
        ▼
  (optional) delete source case_dir
        │
        ▼
  per-case Orthanc registration: the executor issues POST /indexer/scan
  scoped to this case's study folders (no timer, no restart); an
  end-of-run sanity pass re-verifies every series
        │
        ▼
  (cold_path_cache, automatic by default: cleanup_loose_after_indexing=true)
  each case's loose DICOMs are deleted once indexed + verified; the archive
  stays canonical. Set false to keep loose files for a manual
  scripts/cold_storage/cleanup_loose_dicoms.py pass.
```

Per-series compression failures are **non-fatal**: the case completes with
its successful rows, failed rows keep `dicom_archive_path = NULL`, and a
JSON report lands in `image_ingestion_protocols/logs/compression_failures_*.json`.
Retry with `scripts/cold_storage/archive_all_series.py --patient <id>`.

Details: [`image_ingestion_protocol.md`](image_ingestion_protocol.md).

---

## 8. Auth model

End users authenticate only to Web App (the PostgreSQL `users` table is the
single source of truth for end-user identity); admins and the service account
also have `orthanc_users.json` entries for direct Orthanc access. Web App
reverse-proxies `/ohif/*` and `/dicom-web/*` to Orthanc with the service-account
credential, so end users never present credentials to Orthanc directly. Every
non-admin also carries a per-user `allowed_datasets` scope.

Full auth model — end-user/admin/service-account roles, provisioning via
`scripts/admin/manage_users.py`, and §5.4 dataset-level authorization — is
canonical in [`architecture.md`](architecture.md) §5.

---

## 9. Portable vs site-specific

The Orthanc + OHIF + Explorer 2 + custom-indexer layer, the Web App, the
`cold_path_cache` stack, and `manage_users.py` / `init_orthanc_db.sh` are
portable; `image_ingestion_protocols/` is SSC-specific. The full portability
breakdown and fresh-deployment guidance are canonical in
[`architecture.md`](architecture.md) §7.

---

## 10. Where to look next

| If you need to... | Read |
|---|---|
| Trace an HTTP route end-to-end | [`architecture.md`](architecture.md) §4, [`web_app.md`](web_app.md) |
| Understand the UI table / inline edit / preview | [`web_app_frontend.md`](web_app_frontend.md) |
| Query or migrate the schema | [`data_stores.md`](data_stores.md) |
| Deploy fresh on a new host | [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md) |
| Build or patch the custom Orthanc image | [`../../orthanc-indexer-patched/README.md`](../../orthanc-indexer-patched/README.md) |
| Run day-2 operator commands | [`../operations/commands.md`](../operations/commands.md) |
| Warm / evict / clean up cold storage | [`../cold_storage/runbook.md`](../cold_storage/runbook.md) |
| Convert DICOM → NIFTI or inspect a cold archive | [`../recipes/dicom_processing.md`](../recipes/dicom_processing.md) |
| Ingest new imaging data | [`image_ingestion_protocol.md`](image_ingestion_protocol.md) |
