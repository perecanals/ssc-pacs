# SSC fork patches to the Orthanc Folder Indexer

This is a fork of the upstream Orthanc **Folder Indexer** plugin with two SSC-specific
changes. Every edit in the source is marked with a `// SSC fork:` comment — run
`grep -rn "SSC fork" src/Sources/` to see them all.

Image tag: **`ssc-orthanc:patched-indexer`** (built by `Dockerfile`; plugin version
`1.1.0`, see `-DORTHANC_PLUGIN_VERSION` in the Dockerfile). Rebuild + deploy:

```bash
cd orthanc-indexer-patched
docker build -t ssc-orthanc:patched-indexer .
cd ../stanford-stroke-pacs
scripts/orthanc/dc.sh down && scripts/orthanc/dc.sh up -d
docker logs ssc-orthanc | grep -E "RemoveMissingFiles|on-demand scans|monitor idle"
```

Both patches touch only `src/Sources/Plugin.cpp` and `src/Sources/IndexerDatabase.{h,cpp}`
(no new files, no CMake change).

---

## Patch 1 — `RemoveMissingFiles` flag (cold-storage eviction accommodation)

**Why.** Upstream's scan has a third step (`LookupDeletedFiles()`) that removes index
rows for files no longer on disk. In `cold_path_cache` mode, loose DICOMs are
deliberately **evicted** (deleted, `*.tar.zst` retained) after use, so the stock indexer
eroded Orthanc's index (series count fell 13,801 → 8,810 → …). We need evicted files to
**stay** indexed so OHIF finds them and the external warm/evict machinery restores them
on demand.

**What changed** (`Plugin.cpp`): a `removeMissingFiles_` global (default `true` =
upstream behavior), read from config `Indexer.RemoveMissingFiles`, and the
`LookupDeletedFiles()` call in the monitor loop guarded behind `if (removeMissingFiles_)`.

**Config.** `orthanc.json` → `"Indexer": { "RemoveMissingFiles": false }`. Startup banner:
`Indexer plugin: RemoveMissingFiles=false — files missing from disk will be KEPT …`.

---

## Patch 2 — On-demand scoped scan endpoint (scalable indexing)

**Why.** The indexer only discovers new files by continuously re-walking every dir in
`Indexer.Folders` each `Interval`. With the whole `/dicom-data` tree (millions of files
over virtiofs) a full pass is glacial — O(whole tree), never scales — and if it runs
during ingestion it can OOM Orthanc. In `cold_path_cache` the only event that ever needs
indexing is ingestion (warm/evict is index-neutral thanks to Patch 1), and we already
know the new paths from `image_series.dicom_dir_path`. So instead of a continuous
whole-tree scan we trigger a **scoped** scan of exactly the new subtrees. Doing that by
editing `orthanc.json` + restarting was fragile (interrupted runs left the config
mutated); a REST trigger is clean — no config edits, no restarts, nothing left dirty.

**What changed** (`Plugin.cpp`, `IndexerDatabase.{h,cpp}`):
- `ScanFolders(folders, stop, stats)` — the DFS body factored out of `MonitorDirectories`,
  now shared by the continuous monitor and the endpoint. `ProcessFile` returns a `bool`
  (was it registered) so scans can count registrations.
- `IndexerDatabase::RemoveFilesUnderPrefix(prefix)` — indexed range delete
  (`path >= prefix+'/' AND path < prefix+'0'`, PK-indexed) used by the `Force` path.
- Concurrency: a `scanSerializer_` mutex (the monitor and an on-demand scan never run at
  once — registration is serial) and an `OnDemandScanState onDemand_` (busy flag, worker
  thread, counters, timestamps) under its own mutex. `OnChangeCallback` joins the worker
  on `OrthancStopped`.
- Empty/absent `Indexer.Folders` is now **valid** (upstream threw): the continuous
  monitor then scans nothing and the endpoint is the trigger. Steady-state config is
  `"Folders": []`.
- Security: `Indexer.ScanRoots` (list, default `["/dicom-data"]`) allow-list; request
  folders must be absolute, `..`-free, and at/under a scan root, else `403`.

**API.**
- `POST /indexer/scan` body `{"Folders": ["/dicom-data/…", …], "Force": <bool>}` — starts
  an async scoped scan and returns `200 {"status":"started"}`; `409` if a scan is already
  running; `400` bad body; `403` folder outside `ScanRoots`. `Force=true` first drops the
  folders' index rows so "orphaned-row" series (row present but the instance was never
  registered, e.g. a POST that failed during an OOM) are re-registered; it's a harmless
  0-row delete otherwise.
- `GET /indexer/scan` → `{"busy", "folders", "filesProcessed", "registered", "startedAt",
  "finishedAt"}` — poll until `busy=false`.

Subject to Orthanc's normal REST auth. The Python client is
`stanford-stroke-pacs/scripts/cold_storage/scoped_index.py` (used by the ingestion
pipeline and by `scripts/cold_storage/reindex_missing_series.py`).

**Config.** `orthanc.json` steady state:
`"Indexer": { "Enable": true, "Folders": [], "RemoveMissingFiles": false,
"ScanRoots": ["/dicom-data"] }`. Startup banner (with empty Folders):
`no static 'Folders' configured — continuous monitor idle; use POST /indexer/scan …`.
