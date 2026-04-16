# System overview

**Purpose:** One place to see the whole PACS stack — the custom SSC research
database, Orthanc, OHIF, the Companion, and the cold-storage layer — and how
they fit together. Deliberately overview-level; link out for detail.

- Narrative on service roles, portability: [`architecture.md`](architecture.md)
- Runtime / packaging / ports: [`runtime_and_config.md`](runtime_and_config.md)
- Database tables and columns: [`data_stores.md`](data_stores.md)
- Cold storage design and rationale: [`../cold_storage/design.md`](../cold_storage/design.md)
- Cold storage operations: [`../cold_storage/runbook.md`](../cold_storage/runbook.md)
- Ingest pipeline: [`image_integration_protocol.md`](image_integration_protocol.md)

---

## 1. Mental model in one paragraph

A single host runs one PostgreSQL server (two logical DBs), one Docker
container (Orthanc with a custom Folder Indexer), and one native service
(the Companion, FastAPI + React). The DICOM payload lives on the host
filesystem — either as loose files (legacy mode) or as per-series
`*.tar.zst` archives that are extracted on demand (`cold_path_cache` mode,
current production). Orthanc serves OHIF and Orthanc Explorer 2 over its
index of that filesystem. The Companion is a research UI that reads
upstream metadata tables, writes multi-level annotations, and embeds OHIF
for row-by-row image review. Users reach both services through an SSH
tunnel.

---

## 2. Topology

```
            ┌─────────────────────────────────────────────────────────────────┐
            │                         Host: stroke                            │
            │                                                                 │
 Browser ───┼──► :8043  ssc-companion.service  (FastAPI + React, systemd)    │
 (via SSH   │         │                                                       │
  tunnel)   │         │  ┌──────────── service-to-service ─────────────┐     │
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
            │  /DATA2/pacs_imaging_data/     ← loose DICOMs (hot)             │
            │  /DATA2/pacs_imaging_data_compressed/  ← *.tar.zst (cold)       │
            │                                                                 │
            └─────────────────────────────────────────────────────────────────┘
```

User-facing URLs:

| URL | Served by |
|-----|-----------|
| `http://localhost:8043/` | Companion landing page |
| `http://localhost:8043/app/` | Companion React app (annotation workflow) |
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
  [`/home/perecanals/pacs/orthanc-indexer-patched/`](../../../orthanc-indexer-patched/README.md).
- Image = `orthancteam/orthanc:latest` + a patched Folder Indexer `.so`
  that honours `RemoveMissingFiles: false` (required by `cold_path_cache`).
- Storage backend is the Folder Indexer plugin itself
  (`ORTHANC__POSTGRESQL__ENABLE_STORAGE=false`). DICOM instances are never
  copied into Orthanc's own storage — they're referenced by filesystem path
  inside `orthanc_db`.
- AET `SSC`, DICOM port `4242`, HTTP `8042`.

### 3.2 OHIF

OHIF is bundled with Orthanc as the `Ohif` plugin (`/ohif` route).
It consumes DICOMweb (`/dicom-web/`) served by Orthanc. The Companion
does not embed its own OHIF build — it builds a URL to Orthanc's OHIF
(`/ohif/viewer?StudyInstanceUIDs=...&SeriesInstanceUIDs=...`) and loads
it in an iframe inside the Companion's preview pane.

### 3.3 Companion (`ssc-companion.service`)

Role: research-oriented browsing + annotation UI that Orthanc Explorer 2
doesn't cover.

- FastAPI app (`companion/app.py`) served by uvicorn on `:8043`.
- Serves the pre-built React frontend (`companion/dist/`) as static files.
- systemd unit `ssc-companion.service`, runs in the `pacs` conda env.
- Reads `companion/config.py` → repo-root `config.toml` for non-secrets
  (storage mode, paths, session length).
- Reads secrets from repo-root `.env`.
- Talks to both `stanford-stroke` (research data + own tables) and Orthanc
  (service-to-service REST for OHIF link resolution and the occasional
  lookup).

### 3.4 Custom Folder Indexer plugin

Lives at `/home/perecanals/pacs/orthanc-indexer-patched/`. Upstream is the
[Orthanc Folder Indexer plugin](https://orthanc.uclouvain.be/book/plugins/indexer.html);
the fork adds one config flag:

```json
"Indexer": {
  "Enable": true,
  "Folders": ["/dicom-data"],
  "Interval": 60,
  "RemoveMissingFiles": false
}
```

When `RemoveMissingFiles: false`, the scan loop does **not** remove DICOM
instances whose files have disappeared. That keeps Orthanc's index stable
during eviction/rewarm cycles — foundational for `cold_path_cache`.

---

## 4. Data stores

### 4.1 PostgreSQL — two logical databases

One PostgreSQL server hosts both. Connection params and credentials are in
`.env` (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, plus
`PG_ORTHANC_*` for the Orthanc DB).

```
┌─ orthanc_db ─────────────────────────────────────┐
│  owned by Orthanc PostgreSQL plugin              │
│  (do not mutate except via                       │
│   scripts/orthanc/enrich_orthanc.py)             │
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
│  ├── lvo_clinical_data     (patient level)       │
│  ├── image_study           (study level)         │
│  └── image_series          (series level)        │
│       ├── dicom_dir_path                         │
│       ├── dicom_archive_path   ← cold storage    │
│       ├── nifti_path           (legacy mode only)│
│       └── import_id, import_label                │
│                                                  │
│  Companion-owned:                                │
│  ├── users              (bcrypt + is_admin)      │
│  ├── user_preferences   (JSONB, per-level)       │
│  ├── annotations        (level='patient'|        │
│  │                       'study'|'series')       │
│  ├── label_definitions  (level-aware)            │
│  └── snapshot_patients / _studies / _seriess     │
│                                                  │
│  Cold storage bookkeeping:                       │
│  ├── cache_state        (per-study hot/cold)     │
│  └── orthanc_resource_map  (legacy; unused in    │
│                             cold_path_cache)     │
└──────────────────────────────────────────────────┘
```

See [`data_stores.md`](data_stores.md) for column-by-column detail.

### 4.2 Filesystem

```
/DATA2/
├── pacs_imaging_data/                (legacy_dicom_root — transient in
│                                      cold_path_cache mode, permanent in
│                                      legacy mode. Bind-mounted read-only
│                                      into the Orthanc container as
│                                      /dicom-data.)
│   └── {patient_id}/
│       └── {studyinstanceuid}/
│           └── {seriesdescription}/
│               └── {seriesinstanceuid}/
│                   ├── DICOM/               ← dicom_dir_path
│                   └── NIFTI/image.nii.gz   ← legacy mode only
│
└── pacs_imaging_data_compressed/    (cold_archive_root — canonical in
    │                                 cold_path_cache mode)
    └── {patient_id}/
        └── {studyinstanceuid}/
            └── {seriesdescription}/
                └── {seriesinstanceuid}/
                    └── DICOM.tar.zst        ← dicom_archive_path
```

The archive path deterministically mirrors the loose path — swap
`legacy_dicom_root` for `cold_archive_root` and replace the leaf `DICOM/`
with `DICOM.tar.zst`. That makes `resolve_series_archive()` work without
a DB lookup when `dicom_archive_path` is NULL.

Archive format: flat — DICOM files sit at the archive root, no `DICOM/`
directory wrapper.

---

## 5. Storage modes

Controlled by `config.toml` `[storage].mode`. Two values:

| | `legacy` | `cold_path_cache` (production) |
|---|---|---|
| Canonical store | loose DICOMs in `legacy_dicom_root` | `*.tar.zst` in `cold_archive_root` |
| Hot cache | same as canonical | `legacy_dicom_root` (transient; empty when everything is cold) |
| Orthanc index | populated by routine Folder Indexer scans | **permanent** — never eroded thanks to `RemoveMissingFiles: false` |
| Warming | N/A | extract archive → `dicom_dir_path` |
| Eviction | N/A | rmtree `dicom_dir_path`; index unchanged |
| NIFTI produced by integration protocol | yes (`NIFTI/image.nii.gz` sibling) | no — on-demand via `scripts/dicom/dicom_to_nifti.py` |
| Requires custom Orthanc image | no | yes (`ssc-orthanc:patched-indexer`) |

See [`../cold_storage/design.md`](../cold_storage/design.md) for why
`cold_path_cache` needs the patched indexer and why the previous
`cold_cache` mode was removed.

---

## 6. Request flows

### 6.1 User opens a study from the Companion

```
  click row in DataTable
      │
      ▼
  Companion.jsx → resolveOhifViewerUrl(studyUID)
      │
      ├── GET /api/ohif-link/{studyUID}           → backend
      │     ├── read cache_state.status           (cold_path_cache mode)
      │     ├── if 'cold'  → {status:'cold'}
      │     ├── if 'warming' → {status:'warming'}
      │     ├── if 'hot' → defensive FS probe:
      │     │                 if dicom_dir_path actually on disk:
      │     │                    POST Orthanc /tools/lookup
      │     │                    → build /ohif/viewer?... URL
      │     │                    → {status:'ready', url}
      │     │                 else:
      │     │                    clear stale cache_state, treat as cold
      │     └── legacy mode: skip cache_state, just lookup + build URL
      │
      ├── (if cold) POST /api/studies/{studyUID}/warm
      │     → cache_manager.warm_study()
      │         ├── pg advisory lock on studyUID
      │         ├── mark cache_state.status='warming'
      │         ├── for each series: untar_zst(archive, dicom_dir_path)
      │         └── mark cache_state.status='hot'
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

Both modes emit a `.zip` whose top-level folder is
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

`image_integration_protocols/` is the SSC-specific pipeline. One run per
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
  copy DICOMs →  legacy_dicom_root/.../DICOM/
        │
        ▼
  (cold_path_cache only) compress each series →
        cold_archive_root/.../DICOM.tar.zst
        │
        ▼
  (legacy only) create NIFTI sibling
        │
        ▼
  upsert image_study + image_series
        │
        ▼
  (optional) delete source case_dir
        │
        ▼
  Folder Indexer picks up new loose files on next scan (≤ Interval s)
  and adds them to Orthanc's index
        │
        ▼
  (cold_path_cache only, manual step) scripts/cold_storage/cleanup_loose_dicoms.py
  removes loose copies once Orthanc has indexed them and the archive
  is verified intact
```

Per-series compression failures are **non-fatal**: the case completes with
its successful rows, failed rows keep `dicom_archive_path = NULL`, and a
JSON report lands in `image_integration_protocols/logs/compression_failures_*.json`.
Retry with `scripts/cold_storage/archive_all_series.py --patient <id>`.

Details: [`image_integration_protocol.md`](image_integration_protocol.md).

---

## 8. Auth model

Two independent auth systems coordinated by one tool (`scripts/admin/manage_users.py`):

| System | Credential store | Verified against |
|---|---|---|
| Orthanc (Explorer 2, OHIF, REST, DICOMweb) | `orthanc_users.json` (plaintext, required by Orthanc) | `RegisteredUsers` block |
| Companion (`/app`, `/api/*` writes) | `users` table (bcrypt hashes) + JWT cookie | FastAPI dependency |

`scripts/admin/manage_users.py`:
- creates/updates bcrypt rows in `users`
- regenerates `orthanc_users.json`
- updates `.env`'s `ORTHANC_ADMIN_PASSWORD` when managing the admin user,
  so Companion's service-to-service Orthanc calls keep working

The Companion calls Orthanc using `ORTHANC_ADMIN_*` from `.env`; end users
never hit Orthanc's REST API directly through the Companion.

---

## 9. Portable vs site-specific

| Layer | Portable | Site-specific |
|---|---|---|
| Orthanc + OHIF + Explorer 2 + custom indexer | ✅ | |
| Companion app (FastAPI + React) | ✅ | |
| `cold_path_cache` stack (archiver, cleanup, cache_manager) | ✅ | |
| `scripts/admin/manage_users.py`, `init_orthanc_db.sh` | ✅ | |
| `stanford-stroke` schema (expects `lvo_clinical_data`, `image_study`, `image_series`) | schema shape portable | column conventions SSC-ish |
| `image_integration_protocols/` | | ❌ assumes SSC layout + metadata rules |
| `scripts/orthanc/enrich_orthanc.py` | | ❌ specific to an anonymised-headers deployment |

For a fresh deployment with an equivalent metadata-ingest pipeline, the
Companion + Orthanc + cold storage stack drops in cleanly.

---

## 10. Where to look next

| If you need to... | Read |
|---|---|
| Trace an HTTP route end-to-end | [`architecture.md`](architecture.md) §4, [`companion.md`](companion.md) |
| Understand the UI table / inline edit / preview | [`companion_frontend.md`](companion_frontend.md) |
| Query or migrate the schema | [`data_stores.md`](data_stores.md) |
| Deploy fresh on a new host | [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md) |
| Build or patch the custom Orthanc image | [`../../../orthanc-indexer-patched/README.md`](../../../orthanc-indexer-patched/README.md) |
| Run day-2 operator commands | [`../operations/commands.md`](../operations/commands.md) |
| Warm / evict / clean up cold storage | [`../cold_storage/runbook.md`](../cold_storage/runbook.md) |
| Convert DICOM → NIFTI or inspect a cold archive | [`../recipes/dicom_processing.md`](../recipes/dicom_processing.md) |
| Ingest new imaging data | [`image_integration_protocol.md`](image_integration_protocol.md) |
