# Cold storage — operator runbook

**Purpose:** How to build, deploy, and operate the cold storage stack.
For the design rationale see [`design.md`](design.md). For how new data is
ingested see [`../reference/image_ingestion_protocol.md`](../reference/image_ingestion_protocol.md).

---

## Storage modes

Controlled by `[storage].mode` in `config.toml` at the repo root:

- **`legacy`** — Orthanc indexes loose DICOMs under `dicom_data_root`. No
  cache manager, no archives involved at runtime. Ingestion protocol writes
  loose files only (compression step skipped).
- **`cold_path_cache`** (current production mode) — Canonical payload is
  `*.tar.zst` under `cold_archive_root`. Warm extracts archives back to the
  original `dicom_dir_path`; evict deletes those files. Requires the
  **custom Orthanc image** with the patched Folder Indexer plugin.

The old `cold_cache` mode (extract to a separate hot-cache dir + REST
re-ingest) has been removed. See [`design.md`](design.md) §Migration history.

---

## Prerequisites for `cold_path_cache`

1. **Custom Orthanc image built:** `ssc-orthanc:patched-indexer` must exist
   on the host. See §Build the patched Orthanc image below.
2. **`orthanc.json`** has `"Indexer": { "RemoveMissingFiles": false }` inside
   the `Indexer` block.
3. **`docker-compose.yml`** uses `image: ssc-orthanc:patched-indexer`.
4. **Orthanc has indexed the complete legacy tree at least once** — so every
   `dicom_dir_path` in `image_series` is present in Orthanc's main DB. This
   is true after the first `docker compose up -d` with all files on disk.
5. **Archives exist** for every series that will be served cold. Run
   `scripts/cold_storage/archive_all_series.py` (see below) and verify
   `dicom_archive_path` is populated for all rows.

---

## Build the patched Orthanc image

```bash
cd /home/perecanals/ssc-pacs/orthanc-indexer-patched
docker build -t ssc-orthanc:patched-indexer .
```

Build takes 1–2 minutes. Output image is ~750 MB.

Verify the patch is compiled in:

```bash
docker run --rm --entrypoint /bin/sh ssc-orthanc:patched-indexer -c \
  "strings /usr/share/orthanc/plugins/libOrthancIndexer.so | grep RemoveMissingFiles"
```

Should print `RemoveMissingFiles` and the startup banner strings.

See [`../../../orthanc-indexer-patched/README.md`](../../../orthanc-indexer-patched/README.md)
for the full rationale and ABI notes (the builder base must match the
runtime `orthancteam/orthanc:latest` OS).

---

## Switch to `cold_path_cache` — full procedure

**Assumes you are starting from `legacy` mode with loose DICOMs in place and
Orthanc indexed.**

### 1. Archive everything

```bash
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs
conda activate pacs

# Preview first
python scripts/cold_storage/archive_all_series.py --dry-run

# Full run; workers=4 is a reasonable starting point
python scripts/cold_storage/archive_all_series.py --workers 4

# Verify coverage
psql -d stanford-stroke -c "
  SELECT COUNT(*) FILTER (WHERE dicom_archive_path IS NOT NULL) AS archived,
         COUNT(*) AS total FROM image_series;"
```

Both counts should match. Script is idempotent — rerun safely.

If a previous ingestion run reports "N/M series failed to compress", it
writes a JSON report to `image_ingestion_protocols/logs/compression_failures_*.json`
with per-series details. Those rows have `dicom_archive_path = NULL`. To
find and retry them:

```bash
python scripts/cold_storage/list_unarchived_series.py --count
python scripts/cold_storage/list_unarchived_series.py --patient <id>   # inspect
python scripts/cold_storage/archive_all_series.py --patient <id>        # retry (idempotent)
```

`scripts/cold_storage/cleanup_loose_dicoms.py` already filters out NULL-archive rows, so
failed series are safe from accidental cleanup.

NIFTIs are **not** generated during ingestion in cold_path_cache mode.
See [`../recipes/dicom_processing.md`](../recipes/dicom_processing.md) for
the on-demand workflow via `scripts/dicom/dicom_to_nifti.py`.

### 2. Deploy the patched Orthanc image

```bash
# Build (once)
cd /home/perecanals/ssc-pacs/orthanc-indexer-patched
docker build -t ssc-orthanc:patched-indexer .

# Edit docker-compose.yml
#   image: ssc-orthanc:patched-indexer

# Edit orthanc.json — add "RemoveMissingFiles": false to the Indexer block

# Swap (use the dc.sh wrapper — it resolves the DICOM mount from config.toml)
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs
scripts/orthanc/dc.sh down
scripts/orthanc/dc.sh up -d

# Verify the patch banner at startup
docker logs ssc-orthanc | grep -i RemoveMissingFiles
```

Expected log line:
```
Indexer plugin: RemoveMissingFiles=false — files missing from disk
  will be KEPT in Orthanc's index (cold-storage mode)
```

### 3. Switch the web app to cold_path_cache

Edit `config.toml`:
```toml
[storage]
mode = "cold_path_cache"
dicom_data_root = "/DATA2/pacs_imaging_data"
cold_archive_root = "/DATA2/pacs_imaging_data_compressed"
eviction_ttl_hours = 24
```

Restart the web app:
```bash
sudo systemctl restart ssc-web-app
```

### 4. Validate warm/evict in the UI

Log in at `http://localhost:8043/app/` and click a few studies. You should
see a brief "Warming imaging cache…" spinner (seconds), then OHIF loads
normally.

Optional API-level test:
```bash
source .env
SUID="<some study>"
# Ensure cold first
curl -X POST -b cookies.txt http://localhost:8043/api/studies/$SUID/evict
curl      -b cookies.txt http://localhost:8043/api/studies/$SUID/cache-status
# Warm (returns 202 immediately; extraction runs in the background)
curl -X POST -b cookies.txt -w "%{http_code}\n" \
    http://localhost:8043/api/studies/$SUID/warm
# Wait for hot
curl      -b cookies.txt http://localhost:8043/api/studies/$SUID/cache-status
# Ready
curl      -b cookies.txt http://localhost:8043/api/ohif-link/$SUID
```

### 5. Remove loose files (the point of no return)

Once you're satisfied that warm/evict cycles work in the UI, run the
cleanup script. It only deletes a series' loose dir when ALL three of these
hold:

1. The archive exists at `dicom_archive_path` and is non-empty
2. The archive's file count matches the loose dir's file count
3. The series' `SeriesInstanceUID` is present in Orthanc's `dicomidentifiers`
   table (i.e. the patched Folder Indexer has indexed it)

With `--execute` it also deletes the cleaned series' `series_cache_state`
rows (absence reads as cold), so the UI immediately reflects the removal.

```bash
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs
conda activate pacs

# Dry-run by default — see exactly what would be deleted
python scripts/cold_storage/cleanup_loose_dicoms.py

# Limit to one patient for an initial test
python scripts/cold_storage/cleanup_loose_dicoms.py --patient 4-0551

# Actually delete
python scripts/cold_storage/cleanup_loose_dicoms.py --execute

# After cleanup, verify Orthanc still has the full index
source .env
curl -s -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" \
  http://localhost:8042/statistics | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f'series={d[\"CountSeries\"]} instances={d[\"CountInstances\"]}')"

# Test warm in the UI again — should still work end-to-end.
```

For routine cleanup after each ingestion run, schedule the script via
cron with `--quiet --no-deep-verify` for fast incremental passes. Run a
deep-verify pass weekly to catch archive corruption.

The script aborts if `STORAGE_MODE != "cold_path_cache"` to prevent
accidental deletion in legacy mode.

---

## Watchdog and disk-space guard

The cache manager has three robustness invariants (see WS 05). All three are
configured in `[storage]` of `config.toml`:

```toml
warming_timeout_minutes      = 30      # stuck-warming watchdog
warming_disk_safety_factor   = 3.0     # required ≈ factor × compressed
warming_disk_min_free_bytes  = 104857600  # plus this much headroom
warm_workers                 = 2       # background extraction pool size
```

`warm_workers` bounds how many extractions run concurrently in
`app.state.warm_executor`. Extra `POST /warm` requests still get 202
immediately; their extractions queue until a worker is free. Sized to
match expected disk throughput — bump it if benchmarks show idle disk
during burst load.

### Stuck-warming watchdog

If a process crashes between archive extraction and the final
`status='hot'` mark, the series' row stays in `status='warming'`. Three layers
recover from this — no manual action is normally needed:

1. **On-demand (`warm_series()`, reached via `warm_study()`):** the next warm
   for that series acquires the per-series advisory lock (the dead warmer's
   lock was released when its connection died), sees `status='warming'` with
   `warming_started_at < now() - warming_timeout_minutes`, logs
   `warm_series: warming watchdog fired …`, and re-warms.
2. **Background reaper (`reap_stale_warming()`):** the eviction loop
   (every 15 min) resets any `warming` series row past `warming_timeout_minutes`
   back to `status='cold'` — but only if `pg_try_advisory_lock` succeeds,
   so a genuinely in-progress (slow) warm is never clobbered. This means a
   stuck row self-heals even if no one warms it again.
3. **UI / status reads:** `get_batch_cache_status` / the patient
   aggregates report a stale `warming` row as `cold`, so the Decompress
   button becomes clickable again immediately (it never gets wedged on a
   permanently disabled "Warming…").

If you want to clear a stuck row without waiting for the reaper, just
trigger a warm manually:

```bash
source .env
SUID="<study uid>"
curl -X POST -b cookies.txt http://localhost:8043/api/studies/$SUID/warm
```

To inspect the rows directly (cache state is now per-series):

```bash
psql -d stanford-stroke -c "
  SELECT seriesinstanceuid, status, warming_started_at,
         now() - warming_started_at AS age, error_message
  FROM series_cache_state
  WHERE status = 'warming';"
```

To force-clear a row (rare; only if the watchdog is somehow not firing
and you cannot trigger a warm):

```bash
psql -d stanford-stroke -c "
  UPDATE series_cache_state
  SET status = 'cold', warming_started_at = NULL, error_message = NULL
  WHERE seriesinstanceuid = '<series uid>';"
```

### Disk-space precheck

The estimate runs in two places:

1. **`POST /api/studies/{uid}/warm` route handler** — calls
   `cache_manager.estimate_warm_disk_space(uid)` *before* submitting the
   extraction to the background executor. If `required > available`, the
   route returns **507 Insufficient Storage** synchronously and no
   background work is queued. This is the path operators see in practice.
2. **Inside `warm_series()` (worker thread, reached via `warm_study()`)** —
   the same check runs again per series as a defensive second line in case
   disk fills between the route precheck and the worker starting. On failure
   the worker marks the series' row `status='cold'` and raises
   `InsufficientDiskSpaceError` to its own log line (no HTTP client is
   listening at this point).

Required free bytes are estimated as
`safety_factor × Σ(compressed archive sizes) + min_free_bytes`. The 507
response body is:

```json
{"detail": {
  "error": "insufficient_disk_space",
  "required_bytes": 12345678,
  "available_bytes":   234567,
  "target": "/DATA2/pacs_imaging_data/<patient>/<study>/<series>"
}}
```

When you see this in the journal:
1. `df -h /DATA2/pacs_imaging_data` to confirm.
2. Identify what's filling the disk — usually a runaway warm fan-out
   from the UI, or a cron job dumping into the same mount.
3. Once free space is restored, retrying the warm succeeds (idempotent;
   the row is at `cold`).

### Transactional eviction

`evict_study()` (a thin wrapper over `evict_series` for the study's series)
deletes a series' `series_cache_state` row **only after** its `rmtree`
succeeds. If `rmtree` fails (permissions, EBUSY, disk error), the row is
left intact, the failure is logged, and the API returns 500. The operator
must clear the underlying cause and retry.

### Health probe

`scripts/cold_storage/cold_storage_health.py` reports:
- count of stuck-warming rows;
- count of orphan `*.warming` directories on disk;
- free disk on `dicom_data_root`;
- distribution of `series_cache_state` rows by `last_accessed_at` bucket.

```bash
# Human output
conda activate pacs
python scripts/cold_storage/cold_storage_health.py

# JSON for monitoring tools
python scripts/cold_storage/cold_storage_health.py --json
```

Exit code is non-zero if any critical condition holds (stuck rows,
orphan dirs, or free disk below `--min-free-bytes`, default 5 GiB).

A systemd timer runs the probe every 15 minutes:

```bash
sudo cp systemd/cold-storage-health.service systemd/cold-storage-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cold-storage-health.timer

systemctl list-timers cold-storage-health.timer
journalctl -u cold-storage-health.service -n 50
```

Failures appear in `journalctl` (the unit exits non-zero) and the JSON
report can be wired into your alerting layer once WS 06 lands.

---

## API reference (cold storage)

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /api/storage-mode` | — | Returns `{"storage_mode": "cold_path_cache" \| "legacy"}` |
| `GET /api/studies/{uid}/cache-status` | — | Aggregate study status over its `series_cache_state` rows |
| `POST /api/studies/{uid}/warm` | yes | Queue extraction in `app.state.warm_executor`; returns **202** immediately. Returns **507** if a synchronous disk-space precheck fails. Watch `cache-status` for `hot`. |
| `POST /api/studies/{uid}/evict` | yes | Delete extracted files, clear the study's series_cache_state rows |
| `GET /api/series/{seriesinstanceuid}/cache-status` | — | `series_cache_state` row for the series |
| `POST /api/series/{seriesinstanceuid}/warm` | yes | Warm a single series (its own row); returns **202**. |
| `POST /api/series/{seriesinstanceuid}/evict` | yes | Evict a single series |
| `POST /api/cache-status/batch` | — | Accepts `study_uids`, `patient_uids`, `series_uids`; returns `studies`, `patients`, `series` `{uid: status}` maps |
| `GET /api/ohif-link/{uid}` | — | Returns `{status: 'cold' \| 'warming' \| 'ready' \| 'error', url}`; with stale-state FS probe |

The frontend's `warmOhif.resolveOhifViewerUrl()` consumes these and handles
the full cold → warm → ready flow transparently. Users clicking a study in
the DataTable never see a raw cold error.

---

## Repairing stale index entries (duplicate-path rot)

Because the patched indexer runs with `"RemoveMissingFiles": false`, it never
prunes `Files` rows whose underlying file disappears. That is **correct** for
cold studies (their files legitimately come and go). But if a series' DICOMs ever
lived under the *wrong* directory before being reorganized — e.g. an earlier
migration/re-ingest placed files under a different study dir, which the indexer
recorded, then the data moved to its canonical `image_series.dicom_dir_path` — an
Orthanc instance can end up with **two `Files` rows**: one valid (correct dir) and
one stale (wrong dir, file gone). The plugin resolves an instance with
`SELECT path FROM Files WHERE instanceId=?` and takes the *first* row, so it
intermittently returns the dead path → Orthanc 500 → **OHIF shows a blank pane**.

`scripts/cold_storage/prune_stale_index_paths.py` detects and removes only those
stale rows. Detection is **structural and warm-state independent**: a row is stale
iff its directory ≠ the DB-canonical dir for the instance's *true*
SeriesInstanceUID (instance→series from Orthanc `/tools/find`; series→dir from
`image_series.dicom_dir_path`). It never consults the filesystem, so it also cleans
stale rows for studies that are currently cold, while leaving their *valid*
(currently-missing) rows intact.

```bash
conda activate ssc-pacs

# Report only (default). One patient, or all.
python scripts/cold_storage/prune_stale_index_paths.py --patient 24-012
python scripts/cold_storage/prune_stale_index_paths.py --json   # all patients + JSON report

# Apply. Briefly STOPS Orthanc, backs up the index DB, deletes stale rows, restarts.
python scripts/cold_storage/prune_stale_index_paths.py --patient 24-012 --execute --yes
python scripts/cold_storage/prune_stale_index_paths.py --execute            # global
```

Safety: dry-run by default; never deletes an instance's last `Files` row (the
`Attachments` table does not cascade); pre-edit DB is backed up to
`maintenance/index-prune-reports/backups/`; an attachment-orphan invariant is
asserted before the edited DB is restored; idempotent (a second run finds 0).
Instances whose *every* row is stale are reported and only removed with
`--delete-orphans` (via Orthanc REST, which cleans both tables). The brief Orthanc
stop is an OHIF/Explorer outage (~1–3 min); a single global `--execute` covers all
patients in one stop/start. JSON reports land in `maintenance/index-prune-reports/`.

---

## Mode comparison

| | `legacy` | `cold_path_cache` |
|---|---|---|
| Canonical store | Loose DICOMs in legacy tree | `*.tar.zst` in archive tree |
| Docker mount | Legacy tree (r/o) | Legacy tree (r/o, mostly empty) |
| Custom Orthanc image | Not required | **Required** (`ssc-orthanc:patched-indexer`) |
| `RemoveMissingFiles` | N/A | Must be `false` in `orthanc.json` |
| Warm API calls | N/A | None to Orthanc; just filesystem extract |
| Evict API calls | N/A | None to Orthanc; just filesystem delete |
| Ingestion workflow | Copy loose files; Orthanc picks up | Copy loose files; compress; Orthanc picks up; optional cleanup |

---

## Switching back to `legacy`

If you need to roll back:

1. Restore all loose files to `/DATA2/pacs_imaging_data` (from backup or by
   batch-extracting archives).
2. Edit `config.toml` → `mode = "legacy"`
3. Restart the web app.
4. You can keep the patched Orthanc image — it's a strict superset of the
   stock one, behaving identically when `RemoveMissingFiles` is unset (the
   flag defaults to `true`).

---

## Key files

| File | Purpose |
|------|---------|
| `web-app/cache_manager.py` | `warm_series`/`evict_series` (per-series primitives), `warm_study`/`warm_patient`/`evict_study` (thin wrappers), `get_cache_status`, `get_series_cache_status`, `run_eviction`, `estimate_warm_disk_space` (route precheck) |
| `web-app/routes/cold_storage.py` | study `POST /warm`/`/evict`/`/cache-status`, series `POST /series/{uid}/warm`/`/evict`/`/cache-status`, `POST /cache-status/batch`, `/storage-mode` |
| `web-app/app.py` | Lifespan owns `app.state.warm_executor` (`ThreadPoolExecutor(max_workers=WARM_WORKERS)`); defensive ohif-link FS probe |
| `web-app/src/api/warmOhif.js` | Frontend warm flow (mode-agnostic cold/warming handling) |
| `scripts/cold_storage/archive_all_series.py` | Offline archiver — populates `dicom_archive_path` for the existing tree |
| `scripts/cold_storage/cleanup_loose_dicoms.py` | Safely delete loose DICOMs whose archive exists and Orthanc has indexed (dry-run by default; cron-friendly) |
| `scripts/cold_storage/list_unarchived_series.py` | List series with loose files but no archive — triage compression failures |
| `scripts/dicom/dicom_to_nifti.py` | On-demand DICOM → NIFTI from a loose dir, a cold archive, or by series UID (optionally warming) |
| `maintenance/scripts/orthanc_path_availability_test.py` (site-local, gitignored) | Automated DICOMweb path-availability probe |
| `maintenance/scripts/orthanc_holdout_case.py` (site-local, gitignored) | Manual OHIF holdout test (hide/restore a patient) |
| `orthanc.json` | `Indexer.RemoveMissingFiles` must be `false` for `cold_path_cache` |
| `docker-compose.yml` | `image: ssc-orthanc:patched-indexer` |
| `config.toml` | `[storage].mode` and paths |
| `orthanc-indexer-patched/` | Source for the custom Folder Indexer plugin |

### Database changes (auto-applied on Web App restart)

- `image_series.dicom_archive_path TEXT` — populated by archiver / ingestion protocol
- `series_cache_state` table (PK `seriesinstanceuid`) — per-series cold/warming/hot/error/queued status; powers warm/evict, the stuck-warming watchdog, and the derived study/patient aggregates. Created by Alembic revision `0010_series_cache_state`, which dropped the former per-study `cache_state` table (backfilling it by fanning each study row to its series) and the dead `orthanc_resource_map` table.

See [`../reference/data_stores.md`](../reference/data_stores.md) for column details.
