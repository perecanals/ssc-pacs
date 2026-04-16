# Upgrading dependencies

The PACS stack pins every layer — Python packages, the conda env, the
upstream Orthanc image, and the Companion frontend's npm deps — so that a
rebuild months from now produces the same bytes. This doc covers how to
bump a pin on purpose.

See [`maintenance/workstreams/02-dependency-pinning.md`](../../../maintenance/workstreams/02-dependency-pinning.md)
for the rationale.

---

## What is pinned, and where

| Layer | File | Form |
|---|---|---|
| Root Python deps | `requirements.txt` | `pkg==X.Y.Z` |
| Companion Python deps | `stanford-stroke-pacs/companion/requirements.txt` | `pkg==X.Y.Z` |
| Conda base env | `stanford-stroke-pacs/environment.yml` | `--from-history` (python + ipykernel) |
| Companion Node deps | `stanford-stroke-pacs/companion/package-lock.json` | npm lockfile |
| Upstream Orthanc image | `orthanc-indexer-patched/Dockerfile` | `FROM orthancteam/orthanc@sha256:...` |
| Patched Orthanc image | `stanford-stroke-pacs/docker-compose.yml` | local tag `ssc-orthanc:patched-indexer` (rebuild-driven) |

---

## Bump a Python package

1. Activate the env:
   ```bash
   conda activate pacs
   ```
2. Edit the pin in `requirements.txt` (root) **and**
   `stanford-stroke-pacs/companion/requirements.txt` if the package appears
   in both (e.g. `psycopg2-binary`, `python-dotenv`). Keep versions in sync
   across the two files.
3. Apply the change:
   ```bash
   pip install -r requirements.txt
   pip install -r stanford-stroke-pacs/companion/requirements.txt
   ```
4. (When WS 07 lands) re-run the test suite. Until then, smoke-test:
   ```bash
   sudo systemctl restart ssc-companion
   sudo journalctl -u ssc-companion -n 50
   curl -sf http://localhost:8043/api/health
   ```
5. Commit the pin change.

## Bump a Node package

1. In `stanford-stroke-pacs/companion/`, edit `package.json` for the desired
   version range, then:
   ```bash
   npm install   # updates package-lock.json
   ```
2. Commit both `package.json` and `package-lock.json`.
3. Rebuild and roll out:
   ```bash
   npm ci && npm run build
   sudo systemctl restart ssc-companion
   ```

Note: production builds use `npm ci` (not `npm install`) so that a
lockfile drift fails the build instead of silently resolving.

## Bump the conda env

The `environment.yml` tracks only explicit installs (`conda env export
--from-history`). To add a package:

1. `conda install -n pacs <pkg>`
2. Regenerate:
   ```bash
   conda env export -n pacs --from-history > stanford-stroke-pacs/environment.yml
   ```
3. Commit.

## Bump the Orthanc image

The upstream base image is pinned by digest in
`orthanc-indexer-patched/Dockerfile`. To bump:

1. Pull a newer tag:
   ```bash
   docker pull orthancteam/orthanc:<tag>
   ```
2. Resolve the new digest:
   ```bash
   docker image inspect orthancteam/orthanc:<tag> \
     --format '{{index .RepoDigests 0}}'
   ```
3. Replace the `FROM orthancteam/orthanc@sha256:...` line in
   `orthanc-indexer-patched/Dockerfile`.
4. If the upstream OS major version changed (check
   `docker run --rm --entrypoint cat orthancteam/orthanc:<tag>
   /etc/os-release`), update the builder stage `FROM ubuntu:<version>` to
   match — libjsoncpp / libboost ABIs must line up or the plugin won't
   load.
5. Rebuild and roll out:
   ```bash
   cd orthanc-indexer-patched
   docker build -t ssc-orthanc:patched-indexer .
   cd ../stanford-stroke-pacs
   docker compose down && docker compose up -d
   docker logs ssc-orthanc | grep RemoveMissingFiles
   ```
   The `RemoveMissingFiles=false` banner confirms the patch still loads.
6. Commit.

## Security / CVE-driven upgrades

Review pinned versions quarterly (or whenever a CVE advisory lands for a
pinned package). For anything load-bearing (psycopg2, fastapi, PyJWT,
bcrypt, requests, Orthanc), treat an upgrade like any other change:
smoke-test, watch for regressions, commit with a note explaining the
advisory.

## Rollback

All pins live in version-controlled files. `git revert <sha>` reverts the
pin bump. For the Orthanc image specifically, the previous `:patched-indexer`
tag is cached locally and can be restored by checking out the old Dockerfile
and rebuilding (or, if the cache is gone, pulling the previous digest from
Docker Hub by the SHA recorded in git history).
