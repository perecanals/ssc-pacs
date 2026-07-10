# Upgrading dependencies

The PACS stack pins every layer — Python packages, the upstream Orthanc
image, and the web app frontend's npm deps — so that a rebuild months from
now produces the same bytes. This doc covers how to bump a pin on purpose.

---

## What is pinned, and where

Python deps are split across **two** pinned requirement sets, each scoped
to a distinct surface (there is no conda `environment.yml` — the env is
provisioned straight from these files):

| Layer | File | Form |
|---|---|---|
| Stack scripts + ingestion pipeline | `stanford-stroke-pacs/requirements.txt` | `pkg==X.Y.Z` |
| Web App runtime | `stanford-stroke-pacs/web-app/requirements.txt` | `pkg==X.Y.Z` |
| Web App dev/test | `stanford-stroke-pacs/web-app/requirements-dev.txt` | `pkg==X.Y.Z` (pytest, ruff, mypy, …) |
| Web App Node deps | `stanford-stroke-pacs/web-app/package-lock.json` | npm lockfile |
| Upstream Orthanc image | `orthanc-indexer-patched/Dockerfile` | `FROM orthancteam/orthanc@sha256:...` |
| Patched Orthanc image | `stanford-stroke-pacs/docker-compose.yml` | local tag `ssc-orthanc:patched-indexer` (rebuild-driven) |

---

## Bump a Python package

1. Activate the env:
   ```bash
   conda activate ssc-pacs
   ```
2. Edit the pin in **every** requirement set where the package appears —
   a shared dep like `psycopg2-binary` or `python-dotenv` is listed in both
   files. Keep versions in sync across them.
3. Apply the change (install whichever sets you touched):
   ```bash
   pip install -r stanford-stroke-pacs/requirements.txt              # stack scripts + ingestion
   pip install -r stanford-stroke-pacs/web-app/requirements.txt      # web app runtime
   ```
4. Re-run the test suite (`make test` from the checkout root), then smoke-test:
   ```bash
   # Linux (systemd):  sudo systemctl restart ssc-web-app
   # macOS (launchd):  sudo launchctl kickstart -k system/com.ssc.webapp
   sudo systemctl restart ssc-web-app
   sudo journalctl -u ssc-web-app -n 50   # macOS: tail -n 50 ~/Library/Logs/ssc-web-app.err
   curl -sf http://localhost:8043/healthz
   ```
5. Commit the pin change.

## Bump a Node package

1. In `stanford-stroke-pacs/web-app/`, edit `package.json` for the desired
   version range, then:
   ```bash
   npm install   # updates package-lock.json
   ```
2. Commit both `package.json` and `package-lock.json`.
3. Rebuild and roll out:
   ```bash
   npm ci && npm run build
   sudo systemctl restart ssc-web-app   # macOS: sudo launchctl kickstart -k system/com.ssc.webapp
   ```

Note: production builds use `npm ci` (not `npm install`) so that a
lockfile drift fails the build instead of silently resolving.

Adding a new Python dependency: pin it in whichever of the two requirement
sets owns that surface (there is no conda `environment.yml`), then `pip
install -r` that file.

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
   scripts/orthanc/dc.sh down && scripts/orthanc/dc.sh up -d
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
