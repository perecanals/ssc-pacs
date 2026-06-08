# orthanc-indexer-patched

A small fork of the [Orthanc Folder Indexer plugin](https://orthanc.uclouvain.be/book/plugins/indexer.html)
with one added config flag: **`RemoveMissingFiles`**.

When set to `false`, the plugin **skips the scan-time cleanup** that removes
DICOM instances from Orthanc's index when their backing files disappear from
disk. This makes the plugin compatible with cold-storage workflows where
files are legitimately moved/deleted and restored on demand.

Default is `true` (fully backward-compatible with upstream behavior).

---

## Why this exists

The upstream Folder Indexer scans its configured folder on an interval and:

1. Adds any new files it finds (→ Orthanc main DB)
2. Updates any files whose size/mtime changed
3. **Removes any DB entries whose files are missing on disk** ← the problem

Step 3 is hardcoded in `Plugin.cpp::LookupDeletedFiles()`. For the Stanford
Stroke PACS `cold_path_cache` workflow, studies are evicted by deleting their
loose DICOMs (they're preserved as `*.tar.zst` archives). Without the patch,
the Folder Indexer's next scan would remove them from Orthanc's index, and
even restoring the files to their original paths wouldn't bring them back —
the index entries are gone.

With `RemoveMissingFiles: false`, the scan loop skips the cleanup pass entirely.
Missing files are ignored; the index stays stable. New files are still picked
up as usual, so routine ingestion continues to work without an Orthanc restart.

---

## The patch

Three small edits to `src/Sources/Plugin.cpp`:

1. A new static global `removeMissingFiles_` (default `true`)
2. Reads the new config option in `OrthancPluginInitialize`:
   ```cpp
   removeMissingFiles_ = indexer.GetBooleanValue("RemoveMissingFiles", true);
   ```
3. Guards the `LookupDeletedFiles()` call in `MonitorDirectories`:
   ```cpp
   if (removeMissingFiles_)
   {
     try { LookupDeletedFiles(); } catch (...) { ... }
   }
   ```

Grep for `SSC fork` in `src/Sources/Plugin.cpp` to find the exact change sites.

Upstream source: `hg clone https://orthanc.uclouvain.be/hg/orthanc-indexer/`

---

## Build

The Dockerfile uses a two-stage build: Ubuntu 25.10 (matching
`orthancteam/orthanc:latest`) to compile the `.so`, then layers it onto the
official Orthanc image.

```bash
cd /home/perecanals/ssc-pacs/orthanc-indexer-patched
docker build -t ssc-orthanc:patched-indexer .
```

Build takes ~1–2 minutes on a modern machine. The output image is tagged
`ssc-orthanc:patched-indexer` and is ~750 MB.

**ABI note:** the builder base must match the runtime image's OS, because
the plugin dynamically links against `libjsoncpp`, `libboost_*`, etc. If you
bump `orthancteam/orthanc:latest` to a new Ubuntu release, update the
`FROM` line in the `builder` stage of `Dockerfile` accordingly. Check with:

```bash
docker run --rm --entrypoint cat orthancteam/orthanc:latest /etc/os-release
```

---

## Deploy

1. In `stanford-stroke-pacs/docker-compose.yml`, the Orthanc service's `image:`
   line points to `ssc-orthanc:patched-indexer`.
2. In `stanford-stroke-pacs/orthanc.json`, the `Indexer` block must set:
   ```json
   "Indexer": {
     "Enable": true,
     "Folders": ["/dicom-data"],
     "Interval": 60,
     "RemoveMissingFiles": false
   }
   ```
3. Restart Orthanc: `cd stanford-stroke-pacs && docker compose down && docker compose up -d`
4. Verify the patch is active: `docker logs ssc-orthanc | grep RemoveMissingFiles`
   — you should see the startup banner:
   ```
   Indexer plugin: RemoveMissingFiles=false — files missing from disk
     will be KEPT in Orthanc's index (cold-storage mode)
   ```

---

## Portability

This fork is designed to be dropped into other deployments that need cold
storage with Orthanc. To transplant it:

1. Copy the `src/` directory and this `Dockerfile` to the target host
2. Build: `docker build -t <your-tag>:patched-indexer .`
3. In your target stack's Orthanc config, set `Indexer.RemoveMissingFiles: false`
4. Point your compose/kustomize/helm to the new image tag

The patch itself is ~15 lines and is trivial to port to newer upstream releases.

---

## License

The upstream plugin is GPL-3.0-or-later. This fork retains that license.
