# orthanc-indexer-patched

A small fork of the [Orthanc Folder Indexer plugin](https://orthanc.uclouvain.be/book/plugins/indexer.html)
with two SSC changes: the **`RemoveMissingFiles`** flag and an **on-demand scoped scan
endpoint** (`POST/GET /indexer/scan`). See **[PATCHES.md](PATCHES.md)** for the full,
canonical description of both (rationale, API, config, rebuild/deploy).

## Why this exists

The upstream indexer's scan loop removes index entries whose backing files are
missing on disk — incompatible with the `cold_path_cache` workflow, where loose
DICOMs are legitimately evicted (kept as `*.tar.zst` archives) and restored on
demand. `RemoveMissingFiles: false` skips that cleanup pass so the index stays
stable across evict/warm cycles; the scoped scan endpoint registers new data
per case without a continuous whole-tree scan. `RemoveMissingFiles` defaults to
`true` (fully backward-compatible with upstream behavior).

Full rationale, API, and change sites: [PATCHES.md](PATCHES.md). Grep for
`SSC fork` in `src/Sources/` to find the exact edits.

Upstream source: `hg clone https://orthanc.uclouvain.be/hg/orthanc-indexer/`

---

## Build

The Dockerfile uses a two-stage build: Ubuntu 25.10 (matching
`orthancteam/orthanc:latest`) to compile the `.so`, then layers it onto the
official Orthanc image.

```bash
cd orthanc-indexer-patched
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
     "Folders": [],
     "Interval": 60,
     "RemoveMissingFiles": false,
     "ScanRoots": ["/dicom-data"]
   }
   ```
   (`Folders: []` = no continuous scan; new data is registered via scoped
   `POST /indexer/scan` calls under `ScanRoots`. A legacy deployment that wants
   the upstream continuous scan sets `Folders: ["/dicom-data"]` instead.)
3. Restart Orthanc: `cd stanford-stroke-pacs && scripts/orthanc/dc.sh down && scripts/orthanc/dc.sh up -d`
   (use the wrapper, not bare `docker compose` — it resolves the DICOM mount from `config.toml`)
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
