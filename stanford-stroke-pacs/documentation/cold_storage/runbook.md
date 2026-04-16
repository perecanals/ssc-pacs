# Cold storage — operator runbook

**Purpose:** How to build, deploy, and operate the cold storage stack.
For the design rationale see [`design.md`](design.md). For how new data is
ingested see [`../reference/image_integration_protocol.md`](../reference/image_integration_protocol.md).

---

## Storage modes

Controlled by `[storage].mode` in `config.toml` at the repo root:

- **`legacy`** — Orthanc indexes loose DICOMs under `legacy_dicom_root`. No
  cache manager, no archives involved at runtime. Integration protocol writes
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
   `scripts/archive_all_series.py` (see below) and verify
   `dicom_archive_path` is populated for all rows.

---

## Build the patched Orthanc image

```bash
cd /home/perecanals/pacs/orthanc-indexer-patched
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
cd /home/perecanals/pacs/stanford-stroke-pacs
conda activate pacs

# Preview first
python scripts/archive_all_series.py --dry-run

# Full run; workers=4 is a reasonable starting point
python scripts/archive_all_series.py --workers 4

# Verify coverage
psql -d stanford-stroke -c "
  SELECT COUNT(*) FILTER (WHERE dicom_archive_path IS NOT NULL) AS archived,
         COUNT(*) AS total FROM image_series;"
```

Both counts should match. Script is idempotent — rerun safely.

If a previous integration run reports "N/M series failed to compress", it
writes a JSON report to `image_integration_protocols/logs/compression_failures_*.json`
with per-series details. Those rows have `dicom_archive_path = NULL`. To
find and retry them:

```bash
python scripts/list_unarchived_series.py --count
python scripts/list_unarchived_series.py --patient <id>   # inspect
python scripts/archive_all_series.py --patient <id>        # retry (idempotent)
```

`scripts/cleanup_loose_dicoms.py` already filters out NULL-archive rows, so
failed series are safe from accidental cleanup.

NIFTIs are **not** generated during integration in cold_path_cache mode.
See [`../recipes/dicom_processing.md`](../recipes/dicom_processing.md) for
the on-demand workflow via `scripts/dicom_to_nifti.py`.

### 2. Deploy the patched Orthanc image

```bash
# Build (once)
cd /home/perecanals/pacs/orthanc-indexer-patched
docker build -t ssc-orthanc:patched-indexer .

# Edit docker-compose.yml
#   image: ssc-orthanc:patched-indexer

# Edit orthanc.json — add "RemoveMissingFiles": false to the Indexer block

# Swap
cd /home/perecanals/pacs/stanford-stroke-pacs
docker compose down
docker compose up -d

# Verify the patch banner at startup
docker logs ssc-orthanc | grep -i RemoveMissingFiles
```

Expected log line:
```
Indexer plugin: RemoveMissingFiles=false — files missing from disk
  will be KEPT in Orthanc's index (cold-storage mode)
```

### 3. Switch the Companion to cold_path_cache

Edit `config.toml`:
```toml
[storage]
mode = "cold_path_cache"
legacy_dicom_root = "/DATA2/pacs_imaging_data"
cold_archive_root = "/DATA2/pacs_imaging_data_compressed"
eviction_ttl_hours = 24
```

Restart the Companion:
```bash
sudo systemctl restart ssc-companion
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
# Warm
curl -X POST -b cookies.txt http://localhost:8043/api/studies/$SUID/warm
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

```bash
cd /home/perecanals/pacs/stanford-stroke-pacs
conda activate pacs

# Dry-run by default — see exactly what would be deleted
python scripts/cleanup_loose_dicoms.py

# Limit to one patient for an initial test
python scripts/cleanup_loose_dicoms.py --patient 4-0551

# Actually delete
python scripts/cleanup_loose_dicoms.py --execute

# After cleanup, verify Orthanc still has the full index
source .env
curl -s -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" \
  http://localhost:8042/statistics | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f'series={d[\"CountSeries\"]} instances={d[\"CountInstances\"]}')"

# Test warm in the UI again — should still work end-to-end.
```

For routine cleanup after each integration run, schedule the script via
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
```

### Stuck-warming watchdog

If a process crashes between archive extraction and the final
`status='hot'` mark, the row stays in `status='warming'`. The next call
to `warm_study()` for that UID:

1. Acquires the per-study advisory lock (the previous warmer's lock was
   released when its connection died).
2. Reads the row; sees `status='warming'` with
   `warming_started_at < now() - warming_timeout_minutes`.
3. Logs a structured warning (`warm_study: warming watchdog fired …`)
   and proceeds to re-warm.

If you want to clear a stuck row without waiting for an inbound warm
request, just trigger one manually:

```bash
source .env
SUID="<study uid>"
curl -X POST -b cookies.txt http://localhost:8043/api/studies/$SUID/warm
```

To inspect the row directly:

```bash
psql -d stanford-stroke -c "
  SELECT studyinstanceuid, status, warming_started_at,
         now() - warming_started_at AS age, error_message
  FROM cache_state
  WHERE status = 'warming';"
```

To force-clear a row (rare; only if the watchdog is somehow not firing
and you cannot trigger a warm):

```bash
psql -d stanford-stroke -c "
  UPDATE cache_state
  SET status = 'cold', warming_started_at = NULL, error_message = NULL
  WHERE studyinstanceuid = '<study uid>';"
```

### Disk-space precheck

Before extracting, `warm_study()` estimates required free bytes as
`safety_factor × Σ(compressed archive sizes) + min_free_bytes`. If the
filesystem holding the target `dicom_dir_path` cannot fit it, the row is
marked `status='cold'` (not `warming`) and the warm raises
`InsufficientDiskSpaceError`. The HTTP layer surfaces this as
**`507 Insufficient Storage`** with a JSON body:

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

`evict_study()` deletes `cache_state` **only after** every `rmtree`
succeeds. If `rmtree` fails (permissions, EBUSY, disk error), the row is
left intact, the failure is logged with the study UID, and the API
returns 500. The operator must clear the underlying cause and retry.

### Health probe

`scripts/cold_storage_health.py` reports:
- count of stuck-warming rows;
- count of orphan `*.warming` directories on disk;
- free disk on `legacy_dicom_root`;
- distribution of `cache_state` rows by `last_accessed_at` bucket.

```bash
# Human output
conda activate pacs
python scripts/cold_storage_health.py

# JSON for monitoring tools
python scripts/cold_storage_health.py --json
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
| `GET /api/studies/{uid}/cache-status` | — | `cache_state` row for the study |
| `POST /api/studies/{uid}/warm` | yes | Extract archives, mark hot |
| `POST /api/studies/{uid}/evict` | yes | Delete extracted files, clear cache_state |
| `GET /api/ohif-link/{uid}` | — | Returns `{status: 'cold' \| 'warming' \| 'ready' \| 'error', url}`; with stale-state FS probe |

The frontend's `warmOhif.resolveOhifViewerUrl()` consumes these and handles
the full cold → warm → ready flow transparently. Users clicking a study in
the DataTable never see a raw cold error.

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
| Integration workflow | Copy loose files; Orthanc picks up | Copy loose files; compress; Orthanc picks up; optional cleanup |

---

## Switching back to `legacy`

If you need to roll back:

1. Restore all loose files to `/DATA2/pacs_imaging_data` (from backup or by
   batch-extracting archives).
2. Edit `config.toml` → `mode = "legacy"`
3. Restart the Companion.
4. You can keep the patched Orthanc image — it's a strict superset of the
   stock one, behaving identically when `RemoveMissingFiles` is unset (the
   flag defaults to `true`).

---

## Key files

| File | Purpose |
|------|---------|
| `companion/cache_manager.py` | `warm_study`, `evict_study`, `get_cache_status`, `run_eviction`, helpers |
| `companion/app.py` | Warm/evict/cache-status endpoints, defensive ohif-link FS probe |
| `companion/src/api/warmOhif.js` | Frontend warm flow (mode-agnostic cold/warming handling) |
| `scripts/archive_all_series.py` | Offline archiver — populates `dicom_archive_path` for the existing tree |
| `scripts/cleanup_loose_dicoms.py` | Safely delete loose DICOMs whose archive exists and Orthanc has indexed (dry-run by default; cron-friendly) |
| `scripts/list_unarchived_series.py` | List series with loose files but no archive — triage compression failures |
| `scripts/dicom_to_nifti.py` | On-demand DICOM → NIFTI from a loose dir, a cold archive, or by series UID (optionally warming) |
| `scripts/orthanc_path_availability_test.py` | Automated DICOMweb path-availability probe |
| `scripts/orthanc_holdout_case.py` | Manual OHIF holdout test (hide/restore a patient) |
| `orthanc.json` | `Indexer.RemoveMissingFiles` must be `false` for `cold_path_cache` |
| `docker-compose.yml` | `image: ssc-orthanc:patched-indexer` |
| `config.toml` | `[storage].mode` and paths |
| `orthanc-indexer-patched/` | Source for the custom Folder Indexer plugin |

### Database changes (auto-applied on Companion restart)

- `image_series.dicom_archive_path TEXT` — populated by archiver / integration protocol
- `cache_state` table — per-study cold/warming/hot/error status
- `cache_state.warming_started_at TIMESTAMPTZ` — added by Alembic revision `0002_warming_started_at`; powers the stuck-warming watchdog (see above)
- `orthanc_resource_map` table — legacy from removed `cold_cache` mode; harmless empty table

See [`../reference/data_stores.md`](../reference/data_stores.md) for column details.
