# Cold storage design — `tar.zst` archives + patched Orthanc indexer

**Purpose:** Design rationale, how the cold storage model works, and why the
deployment requires a custom Orthanc image. For operator steps see [`runbook.md`](runbook.md).

---

## Goal

Reduce filesystem usage for the imaging payload by treating compressed
`*.tar.zst` archives as the **canonical store** and materializing only the
actively-viewed subset back into the filesystem on demand.

Target access pattern: only ~1–2% of series are opened regularly, so the
working set is small even if the total archive is large (600 GB → ~TB scale).

---

## The mental model

Orthanc is an index + viewer over a DICOM tree it does **not own**. Its main
database (PostgreSQL) stores metadata and file paths; the DICOM pixel payload
lives on disk at those paths. OHIF reads pixel data via DICOMweb, which
ultimately reads the underlying files through Orthanc.

**Key insight:** if the files disappear and later come back to the **same
filesystem paths**, OHIF just works again — no re-ingestion, no index
changes. Orthanc's index never moves; it's the files underneath that shift
between "on disk" (hot) and "in archive" (cold).

This is the core of `cold_path_cache`:

| Phase  | What exists on disk | What OHIF sees |
|--------|---------------------|----------------|
| Hot    | Loose DICOMs at `dicom_dir_path` | Working viewer |
| Cold   | Nothing (just the `*.tar.zst` archive under `cold_archive_root`) | Index entry present but reads fail |
| Warming | Files being extracted from archive back to `dicom_dir_path` | — |
| Warm → Hot again | Loose DICOMs restored | Working viewer |

---

## The problem — and why a patched Orthanc image is required

The Orthanc **Folder Indexer plugin** (`libOrthancIndexer.so`) is Orthanc's
`StorageAreaPlugin` in this deployment (with
`ORTHANC__POSTGRESQL__ENABLE_STORAGE=false`). Every `Interval` seconds, the
plugin walks the configured folder and:

1. Adds any new files to Orthanc's index
2. Updates any changed files
3. **Removes any previously-indexed files whose paths are now missing**

Step 3 is incompatible with cold storage. Within a minute of evicting a study,
the Folder Indexer would notice the missing files and delete the corresponding
DICOM instances from Orthanc's main DB. Warming the files back later would
restore them to the right path — but Orthanc's index entries would already be
gone, so `/tools/lookup` by StudyInstanceUID returns empty.

**Empirically observed:** Orthanc's series count drops continuously
(`13,801 → 8,810 → 6,911 → ...`) on a stack running in `cold_path_cache`
mode with most files in a cold archive.

### The fix: fork and patch

`orthanc-indexer-patched/` adds a single config flag `RemoveMissingFiles`
(default `true`, backward-compatible). When set to `false`, the scan loop
skips the deleted-file cleanup pass. New files are still picked up, so
routine ingestion works unchanged. Missing files are simply ignored.

The patch is ~15 lines and lives inside `src/Sources/Plugin.cpp`. See
[`../../../orthanc-indexer-patched/README.md`](../../../orthanc-indexer-patched/README.md)
for the source, build, and deploy instructions.

The deployment uses a custom Docker image `ssc-orthanc:patched-indexer`
which is `orthancteam/orthanc:latest` with the patched `.so` layered on top.

---

## Storage modes

`config.toml` `[storage].mode` controls which mode is active.

### `legacy`

All DICOMs sit uncompressed under `dicom_data_root`. Orthanc Folder Indexer
reads them directly. No cache manager involvement. Baseline pre-migration.

### `cold_path_cache` (current production mode)

- Canonical store: `*.tar.zst` archives at `cold_archive_root`, one per series.
- Hot cache: the legacy tree itself. When a study is warmed, each series'
  archive is extracted back to its original `dicom_dir_path`. When evicted,
  the extracted files are removed.
- Orthanc index: always full. Never modified by warming or eviction.
- Warming does not involve any Orthanc API calls — the index already knows
  about the files.

**Prerequisites:**
- `ssc-orthanc:patched-indexer` image deployed
- `orthanc.json` has `"Indexer": { "RemoveMissingFiles": false }`
- Orthanc has indexed the complete tree at least once (so every legitimate
  `dicom_dir_path` exists in the main DB)

---

## Storage layout

```
cold_archive_root/                    (canonical — never moves)
  {patient_id}/
    {study_uid}/
      {series_desc}/
        {series_uid}/
          DICOM.tar.zst               (one archive per series, flat file tree inside)

dicom_data_root/                    (transient — files come and go)
  {patient_id}/
    {study_uid}/
      {series_desc}/
        {series_uid}/
          DICOM/                      (materialized only when the study is warm)
            <instance UIDs>
```

The archive naming preserves the series' full uncompressed path (except the leaf
`DICOM/` directory becomes `DICOM.tar.zst`). That means we can compute either
direction deterministically without a lookup:

```python
archive_path = cold_root / dicom_dir.relative_to(data_root).parent / f"{dicom_dir.name}.tar.zst"
```

---

## Warm path detail

```
User clicks a row in the web app DataTable
  → Navigator.jsx handlePreviewSelect
  → warmOhif.resolveOhifViewerUrl(studyinstanceuid)
      1. GET /api/ohif-link/{uid}
         Backend checks the study's aggregate state over its
         series_cache_state rows (study is 'hot' only when ALL its
         series are hot — binary readiness):
           - 'cold'    → returns {status: 'cold', url: null, ...}
           - 'warming' → returns {status: 'warming', url: null}
           - 'error'   → HTTP 503
           - 'hot'     → defensive FS probe. If files actually present:
                           touch_access, POST Orthanc /tools/lookup,
                           return {status: 'ready', url: <OHIF URL>}.
                         If probe shows files missing (stale state):
                           delete the study's series_cache_state rows,
                           return as if 'cold'
      2. If url returned → frontend opens OHIF iframe, done.
      3. If status == 'cold' → POST /api/studies/{uid}/warm
                                 (route: routes/cold_storage.api_warm_study)
         If status == 'warming' → poll cache-status
      4. Route handler runs synchronously, in milliseconds:
         a. Disk-space precheck (cache_manager.estimate_warm_disk_space,
            summed over the study's series). If required > available
            → 507 with structured detail; STOP.
         b. Submit cache_manager.warm_study to app.state.warm_executor
            (bounded ThreadPoolExecutor; max_workers from
            [storage].warm_workers, default 2).
         c. Return 202 with {ok: true, queued: true, studyinstanceuid}.
      5. Background worker thread runs warm_study (a thin wrapper that
         resolves the study's series and delegates to warm_series):
         a. Query image_series for all series in this study
         b. For each series, sequentially (never nested):
            - Acquire the series' OWN advisory pg lock
            - Mark its series_cache_state status = 'warming'
            - Compute archive path from dicom_archive_path / dicom_dir_path
            - Extract tar.zst → <dicom_dir_path>.warming (temp sibling)
            - Atomic rename → dicom_dir_path
            - Mark its series_cache_state status = 'hot', warmed_at = now()
              (cache_path = the series' dicom_dir_path)
            - Release the series' lock
         The study's status is a DERIVED AGGREGATE over these series rows
         — it becomes 'hot' only once every series is hot.
      6. Frontend polls cache-status until 'hot'
      7. Retry GET /api/ohif-link/{uid} — now returns ready URL
      8. OHIF loads via DICOMweb; Orthanc reads files from the restored paths
```

No Orthanc API calls during warm itself. The patched Folder Indexer's
background scan will also notice the new files but that's incidental.

### Eviction

```
TTL expires (eviction loop every 15 min) OR user calls POST /api/studies/{uid}/evict
  → cache_manager.evict_study(uid)  (thin wrapper → evict_series over the study's series)
      1. Query image_series for all dicom_dir_path values
      2. For each series: shutil.rmtree its dicom_dir_path
      3. DELETE that series' series_cache_state row
         (Orthanc index untouched; patched indexer keeps the entries.)
```

`touch_access(study)` touches all of a study's series_cache_state rows, so
a whole-study warm ages together for eviction parity.

---

## Database/schema changes

- **`image_series.dicom_archive_path`** (TEXT, nullable) — absolute path to
  the `*.tar.zst` archive for this series. Populated by
  `scripts/cold_storage/archive_all_series.py` and by the image integration protocol.
- **`series_cache_state`** table (PK `seriesinstanceuid`) — per-series status
  (`cold` / `warming` / `hot` / `error` / `queued`), `warmed_at`,
  `last_accessed_at`, `cache_path` (the series' `dicom_dir_path`),
  `warming_started_at`, `error_message`. The series is the single source of
  truth; study/patient warm status is a derived aggregate. Used by
  `warm_series`/`evict_series` (and the `warm_study`/`evict_study` wrappers)
  and `ohif_link`. Replaced the former per-study `cache_state` table
  (Alembic `0010_series_cache_state`).

See [`../reference/data_stores.md`](../reference/data_stores.md) for column details.

---

## Web App runtime

### API

- `POST /api/studies/{uid}/warm` (authenticated) — queue extraction.
  Returns **202** with `{ok, queued, studyinstanceuid}` after a synchronous
  disk-space precheck. Returns **507** if the precheck fails (with
  `{error: 'insufficient_disk_space', required_bytes, available_bytes, target}`).
  The extraction runs in `app.state.warm_executor`; clients observe
  completion via `cache-status` polling.
- `POST /api/studies/{uid}/evict` (authenticated) — manual eviction
- `GET  /api/studies/{uid}/cache-status` — aggregate study status over its
  series_cache_state rows
- `POST /api/series/{seriesinstanceuid}/warm` (authenticated) — warm a single
  series (its own series_cache_state row)
- `POST /api/series/{seriesinstanceuid}/evict` (authenticated) — evict a single
  series
- `GET  /api/series/{seriesinstanceuid}/cache-status` — that series'
  series_cache_state row
- `POST /api/cache-status/batch` — accepts `study_uids`, `patient_uids`, and
  `series_uids`; returns `studies`, `patients`, and `series` `{uid: status}` maps
- `GET  /api/storage-mode` — current storage mode
- `GET  /api/ohif-link/{uid}` — returns `{status, url}` with cold/warming/ready

### Frontend

`web-app/src/api/warmOhif.js` — `resolveOhifViewerUrl()` calls
`/api/ohif-link`, and if the response is `cold` or `warming`, it POSTs
`/warm` (or polls, for warming), then retries the link. `warmStudy()`
treats any 2xx (including 202) as "warming started" and then polls
`/cache-status` until `hot`. Mode-agnostic — no dependency on
`getStorageMode()` for the warm path.

The DataTable also surfaces a per-row **Decompress / readiness badge**
(`components/DataTable/WarmButton.jsx`, driven by `useWarmStatus`'s batched
`cache-status` poll) on patient, study, and series rows in `cold_path_cache`
mode for authenticated users. Patient rows aggregate their studies; study rows
aggregate their series. **Series rows now have their OWN state** —
each series row reflects and triggers *that series'* `series_cache_state`,
and clicking it warms only that series (not its siblings).

A study *open* (no `seriesinstanceuid`) warms the whole study, as before. A
series *preview* (the `seriesinstanceuid` is supplied) keys off that series'
own state and warms just that series — a deliberate improvement so sifting
through individual series is fast.

### Defensive series_cache_state probe

The `ohif_link` endpoint, before trusting a `status='hot'` study, runs a quick
filesystem probe on one series' `dicom_dir_path`. If the files are missing,
it clears the study's stale `series_cache_state` rows and reports `cold` to the
frontend, so the warm flow re-triggers automatically. This protects against
drift when files are moved/deleted out-of-band.

### Eviction loop

Background task in `web-app/app.py` lifespan (only when mode is
`cold_path_cache`). Every 15 minutes, scans for studies past
`eviction_ttl_hours` and calls `evict_study()`.

---

## Archive format and compression

**Format:** `tar` → `zstd` (level 6). One archive per series. Files are
stored **flat** inside the archive (no `DICOM/` directory wrapper); the
leaf directory name is reconstructed from the archive's position in the
tree on extraction.

**Why `tar.zst`:** benchmarking against ZIP showed consistently smaller
archives, faster creation, and faster extraction for studies of the sizes
we see in the PACS. See `benchmarks/dicom_archive_benchmark*.py`.

---

## Operational considerations

### Concurrency

Warming is serialized **per-series** via PostgreSQL advisory locks keyed on
`abs(hashtext(seriesinstanceuid))`. A whole-study warm acquires and releases
each series' lock sequentially (never nested). Multiple concurrent clicks on
the same series result in one warm + everyone else seeing `already_hot` once
files are in place.

### Atomic extraction

Each series' archive extracts to a `.warming` sibling directory; only after
successful extraction is the final `dicom_dir_path` created via an atomic
rename. Partial states never leak to OHIF.

### Post-ingestion workflow

When the image integration protocol runs, it:

1. Copies the new loose DICOMs to `dicom_dir_path`
2. Compresses them to `cold_archive_root` (populates `dicom_archive_path`)
3. Does **not** delete the loose files

The pipeline then **registers just those new subtrees** into Orthanc with a
**scoped** indexer scan (`scripts/cold_storage/scoped_index.py`; always on in
`cold_path_cache`): it temporarily rewrites `orthanc.json`
`Indexer.Folders` to the new dirs, restarts Orthanc so the indexer scans only
those (cost O(new data)), then restores the original config. Because
`RemoveMissingFiles` is `false`, future evictions will not remove the rows.

> **Why not the continuous whole-tree scan?** The Folder Indexer's background scan
> re-walks every dir in `Indexer.Folders` each `Interval`; at millions of files
> over the virtiofs mount a pass is glacial (O(whole tree), never scales), and if
> it runs during ingestion it can OOM Orthanc. In `cold_path_cache` the only event
> that ever needs indexing is ingestion (warm/evict is index-neutral), so
> steady-state `Indexer.Folders` is `[]` (no continuous scan). Backfill any gap
> with `scripts/cold_storage/reindex_missing_series.py`; detect drift with
> `scripts/data_integrity/reconcile.py`.

After indexing is confirmed, loose files for the newly-ingested studies can
be moved out of `dicom_data_root` (they're already in the archive).

### Disk budget

In steady state, `dicom_data_root` only holds studies that are currently
warm. Plan for peak working-set size based on expected concurrent viewers.

---

## Docker mount

The Orthanc bind mount is the legacy root, read-only:

```yaml
- /DATA2/pacs_imaging_data:/dicom-data:ro
```

Orthanc never writes. The Web App (running on the host, outside the
container) is responsible for all writes into the legacy tree during warm
and all deletes during evict.

---

## Migration history (reference)

The path to the current state was not direct:

1. **Initial attempt — `cold_cache` mode** (removed): extract archives to a
   separate hot-cache directory, then re-ingest into Orthanc via
   `POST /instances`. Rejected because re-ingest at warm time was too slow
   and doubled storage.
2. **Second attempt — `cold_path_cache` with stock Folder Indexer**: worked
   briefly but Orthanc's index eroded over time as the Folder Indexer's scan
   loop removed missing files. This was the root cause of flaky warm
   behavior — some studies worked, most didn't.
3. **Third attempt — fork the plugin**: add `RemoveMissingFiles` flag, build
   custom image, deploy. **Current state.** Warm/evict/re-warm works
   reliably end-to-end.

The old `cold_cache` branch of the code is gone; `cache_manager.warm_study`
has a single `cold_path_cache` path.
