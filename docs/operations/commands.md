# Operations commands (cheat sheet)

**Purpose:** Day-2 commands and quick API examples. For first-time deploy see [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md). For runtime/config context see [`../reference/runtime_and_config.md`](../reference/runtime_and_config.md).

**Two "roots".** Script paths (`scripts/...`) are relative to the **stack root**
`stanford-stroke-pacs/`. `make` targets run from the **checkout root**
`/opt/ssc-pacs/ssc-pacs/` (where the `Makefile` lives). Each block below says
which it assumes.

**Service platform.** The reference deployment runs on **Linux via systemd**
(`ssc-web-app.service` for the web app). Command examples lead with the
systemd form; the macOS/launchd equivalents (the `com.ssc.*` LaunchDaemons;
web app = `com.ssc.webapp`) are shown alongside as the alternative where they
differ.

**Destructive-flag polarity.** The mutating cold-storage/label scripts are
**dry-run by default** and only write with `--execute`:
`archive_all_series.py`, `bulk_set_label_values.py`, `rebuild_cache_state.py`,
`prune_stale_index_paths.py`, `reindex_missing_series.py`. (`remove_label.py`
prompts unless you pass `--yes`.)

---

## Server control

### Orthanc (Docker)

```bash
# Use the dc.sh wrapper instead of bare `docker compose`: it resolves the DICOM
# mount from config.toml and selects the macOS override. Bare `docker compose up`
# errors that DICOM_MOUNT_SOURCE is unset.

# Start Orthanc
scripts/orthanc/dc.sh up -d

# Stop Orthanc
scripts/orthanc/dc.sh down

# Restart Orthanc
scripts/orthanc/dc.sh restart

# Full status check (Docker, API, plugins, stats)
./scripts/orthanc/check_status.sh

# Full teardown (removes container, volume, DB) — DESTRUCTIVE
./scripts/admin/teardown.sh
```

### Web App (native service)

**Linux (systemd):**

```bash
sudo systemctl start|stop|restart ssc-web-app
sudo systemctl status ssc-web-app
sudo journalctl -u ssc-web-app -f
sudo scripts/linux/install_systemd.sh   # installs unit templates + enables

# Rebuild frontend after code changes (either platform)
cd web-app && npm run build
sudo systemctl restart ssc-web-app

# Run manually (development, with auto-reload)
cd web-app && uvicorn app:app --port 8043 --reload
```

**macOS (launchd) — alternative:**

```bash
# Restart (the common one)
sudo launchctl kickstart -k system/com.ssc.webapp

# Stop / start
sudo launchctl bootout system/com.ssc.webapp
sudo launchctl bootstrap system /Library/LaunchDaemons/com.ssc.webapp.plist

# Live logs (flat JSON files — pipe through jq)
tail -f ~/Library/Logs/ssc-web-app.log ~/Library/Logs/ssc-web-app.err

# Install / re-render all daemons (one-time or after editing a *.plist.in)
sudo scripts/macos/install_launchd.sh
```

> The service units are installed from `*.in` templates by the installer
> scripts above — not by hand-copying files into `/etc` or `/Library`.

### Whole stack (non-destructive stop / start)

Pause or resume every service at once (does **not** remove containers, volumes,
or data — for the destructive path use `scripts/admin/teardown.sh`). Add
`--dry-run` to any of these to print the sequence without touching anything.

**Linux (systemd):** leaves shared dockerd + host Postgres running.

```bash
sudo scripts/linux/stop_stack.sh            # timers → web app → Orthanc (dc.sh down)
sudo scripts/linux/start_stack.sh           # Orthanc → web app → timers
sudo scripts/linux/stop_stack.sh --retire   # also disable autostart on boot
sudo scripts/linux/start_stack.sh --enable  # start AND re-enable autostart
```

**macOS (launchd) — alternative:** boots out the daemons (handles the
watchdog-before-`colima stop` ordering); Postgres stopped last.

```bash
sudo scripts/macos/stop_stack.sh            # daemons → web app → Orthanc → Colima → Postgres
sudo scripts/macos/start_stack.sh           # bootstrap all daemons (Colima first)
sudo scripts/macos/stop_stack.sh --retire   # also launchctl disable (permanent retirement)
```

---

## User management

End users live in the `users` PostgreSQL table (bcrypt). Web App is the
single login point — its reverse proxy serves OHIF and DICOMweb to any
authenticated user. Admin users are also mirrored into `orthanc_users.json`
so they can reach Orthanc directly on `:8042` as themselves.

```bash
# List all users
python scripts/admin/manage_users.py list

# Add a regular user (DB only) — admin types a temporary password.
# Without --datasets the user sees NO data until granted (deny-by-default).
python scripts/admin/manage_users.py add alice
python scripts/admin/manage_users.py add alice --datasets 'PRECISE,CRISP2/LVO'

# Add an admin user (DB + orthanc_users.json); admins see all datasets
python scripts/admin/manage_users.py add bob --admin

# Reset a user's password (admin-driven; user is forced to change it again)
python scripts/admin/manage_users.py passwd alice

# Replace a user's dataset grants (web-app data visibility)
python scripts/admin/manage_users.py set-datasets alice 'PRECISE,CRISP2/LVO'
python scripts/admin/manage_users.py set-datasets alice --all   # every current dataset
python scripts/admin/manage_users.py set-datasets alice --none  # revoke all access

# Remove a user
python scripts/admin/manage_users.py remove alice

# Verify .env and orthanc_users.json agree on the service account
python scripts/admin/rotate_service_account.py check   # exits non-zero on drift
```

Rename a dataset tag across the `patient` table and every user's grants
(keeps access intact when a cohort is renamed; dry-run default, `--execute`
to apply):

```bash
python scripts/admin/rename_dataset_value.py --from-value lvo --to-value 'CRISP2/LVO'
python scripts/admin/rename_dataset_value.py --from-value lvo --to-value 'CRISP2/LVO' --execute
```

Dataset grants control which patients a non-admin user sees in the web app
(`patient.dataset` overlap; admins bypass). They can also be edited in the
web app's `/admin` page (admin-only). Changes take effect immediately — no
restart needed.

`add` and `passwd` both set the user's `must_change_password` flag to TRUE. On
their next sign-in the Navigator UI redirects them to `/change-password` and
the API blocks every other endpoint with `403 password_change_required` until
they pick a new password. This forced first-login change does **not** ask for
the temporary password again (the user just authenticated with it) — it only
requires the new password, which must differ from the temp one. A later
*voluntary* change still requires the current password. There is no self-service
password reset — a forgotten password requires an admin to run `passwd` and
share a fresh temporary one out-of-band.

Adding, removing, or changing the password of a **non-admin** user only touches
PostgreSQL — no service restart is needed. For **admin** users the script also
updates `orthanc_users.json`; restart Orthanc to pick it up:

```bash
docker restart ssc-orthanc
```

### Rotating credentials

Full runbook (when-to-rotate, verification, failure modes) is
[`secret_rotation.md`](secret_rotation.md). Quick reference — both rotators
prompt for the new password (hidden); add `--generate` to mint a strong random
one and print it once. The secret never touches the command line.

Orthanc service account (`ORTHANC_ADMIN_PASSWORD`, used by the Web App proxy and
host-local scripts):

```bash
python scripts/admin/rotate_service_account.py rotate   # rewrites .env + orthanc_users.json
docker restart ssc-orthanc
sudo systemctl restart ssc-web-app   # macOS: sudo launchctl kickstart -k system/com.ssc.webapp
python scripts/admin/rotate_service_account.py check    # verify in sync
```

Database password (`DB_PASSWORD`, the `stanford-stroke` login for `DB_USER`):

```bash
python scripts/admin/rotate_db_password.py rotate       # ALTER ROLE on the DB + rewrites .env
sudo systemctl restart ssc-web-app   # macOS: sudo launchctl kickstart -k system/com.ssc.webapp
python scripts/admin/rotate_db_password.py check        # verify .env authenticates
```

---

## SSH tunnel (run from local machine)

Ready-made wrappers ship per-OS: `scripts/connectivity/tunnel/linux/tunnel.sh`,
`scripts/connectivity/tunnel/macos/tunnel.command`, and
`scripts/connectivity/tunnel/windows/tunnel.cmd`. Or open it by hand:

```bash
# Open tunnel (includes web app app on 8043)
ssh -N \
  -L 8042:localhost:8042 \
  -L 8043:localhost:8043 \
  -L 4242:localhost:4242 \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=3 \
  <user>@<server>

# Kill tunnel (example: free listener on 8042)
kill $(lsof -ti :8042 -sTCP:LISTEN)
```

---

## Web UI URLs (via tunnel or localhost)

- **Orthanc Explorer 2 (default UI):** http://localhost:8042/ui/app/
- **Web App (landing + app):** http://localhost:8043/ and http://localhost:8043/app/
- **OHIF Viewer:** http://localhost:8042/ohif/
- **Legacy Orthanc Explorer:** http://localhost:8042/app/explorer.html

---

## Monitoring

```bash
# Orthanc container resource usage
docker stats ssc-orthanc --no-stream

# Orthanc container logs (follow) — use dc.sh or the container name directly;
# bare `docker compose logs` fails the DICOM_MOUNT_SOURCE guard.
scripts/orthanc/dc.sh logs -f orthanc
docker logs -f ssc-orthanc

# Recent Orthanc logs only
docker logs --since 5m ssc-orthanc

# Web App logs — Linux (systemd journal)
sudo journalctl -u ssc-web-app -f
# macOS (flat JSON files)
tail -f ~/Library/Logs/ssc-web-app.log ~/Library/Logs/ssc-web-app.err
```

For the full logging/metrics/health reference see
[`observability.md`](observability.md).

---

## Indexing

```bash
# Check indexing progress
curl -s -u admin:<password> http://localhost:8042/statistics | python3 -m json.tool

# Two-DB reconciliation (image_series vs Orthanc index + disk checks)
python scripts/data_integrity/reconcile.py               # human-readable summary
python scripts/data_integrity/reconcile.py --json        # write JSON report
python scripts/data_integrity/reconcile.py --json --quiet # quiet mode (on-demand; not scheduled)
```

---

## Schema migrations (Alembic — `stanford-stroke` only)

From `stanford-stroke-pacs/web-app/` with `conda activate ssc-pacs`. Full
workflow in [`schema_migrations.md`](schema_migrations.md).

```bash
alembic current              # revision applied to this DB (prod may trail repo head until restart)
alembic history              # revision graph
alembic upgrade head         # apply pending revisions (also runs at app startup)
alembic revision -m "<msg>"  # scaffold a new revision (hand-edit it)
```

Do **not** run Alembic against `orthanc_db`.

---

## Ingesting new imaging data

Site-specific SSC pipeline. Copy the example YAML, edit it, then run (from
`stanford-stroke-pacs/image_ingestion_protocols/`, `conda activate ssc-pacs`).
See [`../reference/image_ingestion_protocol.md`](../reference/image_ingestion_protocol.md).

```bash
python execute_image_ingestion_protocol.py [--config path/to/config.yaml]
```

---

## Labels (OE2)

Labels can be managed through the OE2 web UI or via the REST API.

```bash
# List all labels in use
curl -s -u admin:<password> http://localhost:8042/tools/labels

# List studies with a specific label
curl -s -u admin:<password> 'http://localhost:8042/tools/find' \
  -d '{"Level":"Study","Query":{},"Labels":{"BASAL":"Any"}}'

# Add a label to a study (idempotent)
curl -s -u admin:<password> -X PUT http://localhost:8042/studies/<orthanc-id>/labels/<label>

# Remove a label from a study
curl -s -u admin:<password> -X DELETE http://localhost:8042/studies/<orthanc-id>/labels/<label>

# Find studies matching multiple labels (all must match)
curl -s -u admin:<password> 'http://localhost:8042/tools/find' \
  -d '{"Level":"Study","Query":{},"Labels":{"BASAL":"Any","CT":"Any"},"LabelsConstraint":"All"}'
```

### Typical labels

Typical labels in use include:

- **Study type:** `BASAL`, `THROMBECTOMY`, `FOLLOW_UP`, `OTHER`
- **Modality:** `CT`, `MR`, etc.

Users can add custom labels through the OE2 UI. All labels are shared across users.

---

## Web App API examples

Multi-level annotations live in the `stanford-stroke` database; see [`../reference/data_stores.md`](../reference/data_stores.md).

Authenticated endpoints need the JWT cookie. Log in once and reuse the jar:

```bash
curl -s -c cookies.txt -X POST http://localhost:8043/api/login \
  -H 'Content-Type: application/json' -d '{"username":"alice","password":"..."}'
```

```bash
# Rebuild frontend and restart the web app service
cd web-app && npm run build
sudo systemctl restart ssc-web-app   # macOS: sudo launchctl kickstart -k system/com.ssc.webapp

# List all annotation labels via the API
curl -s -b cookies.txt http://localhost:8043/api/labels | python3 -m json.tool

# Label summary with counts
curl -s -b cookies.txt http://localhost:8043/api/labels/summary | python3 -m json.tool

# Search series (optional filters: label, patient_id, modality, description, study_type)
curl -s -b cookies.txt 'http://localhost:8043/api/series?label=hemorrhagic&per_page=10' | python3 -m json.tool

# Add an annotation via API
curl -s -b cookies.txt -X POST http://localhost:8043/api/annotations \
  -H 'Content-Type: application/json' \
  -d '{"seriesinstanceuid":"1.2.3...", "studyinstanceuid":"1.2.3...", "label":"reviewed", "created_by":"alice"}'

# Remove an annotation by ID
curl -s -b cookies.txt -X DELETE http://localhost:8043/api/annotations/42

# Remove a label entirely (definition + annotation rows). Prompts for
# confirmation unless --yes. Run from the stack root; no sudo needed.
./scripts/admin/remove_label.py "My Label Name" --yes

# Bulk-set a label's values from a CSV/Excel table (admin backdoor).
# Creates the label if it does not exist (interactive y/n unless --yes).
# Dry-run by default; add --execute to write. No sudo needed.
python scripts/admin/bulk_set_label_values.py \
    --file /tmp/series_quality.csv --level series \
    --id-column seriesinstanceuid --value-column quality \
    --label series_quality --datatype select --options 'good,acceptable,poor' \
    --execute
```

The backend module map lives in
[`../reference/architecture.md`](../reference/architecture.md); scripts import
`DB_CONFIG` / `get_conn` from `web-app/db.py`.

---

## Testing and code quality

Run from the **checkout root** (`/opt/ssc-pacs/ssc-pacs/`, where the
`Makefile` lives) with `conda activate ssc-pacs`.

```bash
# One-time dev setup (Python deps, Node deps, pre-commit hooks)
make install-dev

# Run all tests (backend + frontend + ingestion)
make test

# Backend only (pytest — needs local Postgres; creates scratch DB)
make test-backend

# Frontend only (vitest — no Postgres needed)
make test-frontend

# Ingestion protocol only (DB-free; e2e cases gated on SSC_INGEST_AUDIT=1)
make test-ingestion

# Lint: ruff on web-app + scripts + image_ingestion_protocols, then eslint on the frontend
make lint
```

**CI:** GitHub Actions runs on every push to `main` and every PR
(`.github/workflows/ci.yml`). Jobs: `lint` (ruff ×3 surfaces),
`backend-tests`, `ingestion-tests`, `frontend-tests` (eslint + vitest),
`frontend-build`. The `mypy` job is advisory (non-blocking).

**Pre-commit hooks:** Installed by `make install-dev`. Runs ruff and prettier
on `web-app/` files automatically before each `git commit`.

For full developer setup details see
[`../guides/installation_and_deployment.md` §8](../guides/installation_and_deployment.md).

---

## Quick Orthanc REST examples

```bash
# Count indexed resources
curl -s -u admin:<password> http://localhost:8042/statistics

# List first 5 studies (JSON)
curl -s -u admin:<password> 'http://localhost:8042/studies?since=0&limit=5&expand'

# Search study by patient ID
curl -s -u admin:<password> 'http://localhost:8042/tools/find' -d '{"Level":"Study","Query":{"PatientID":"15099502"}}'

# Search study by accession number
curl -s -u admin:<password> 'http://localhost:8042/tools/find' -d '{"Level":"Study","Query":{"AccessionNumber":"1116898"}}'

# Orthanc system info
curl -s -u admin:<password> http://localhost:8042/system | python3 -m json.tool
```

---

## Cold storage

See [`../cold_storage/runbook.md`](../cold_storage/runbook.md) for evaluation, archiver, and mode switching.

### Warm / evict / status (per series or per study)

```bash
# Study-level (cache-status is a derived aggregate over the study's series)
curl -b cookies.txt      http://localhost:8043/api/studies/<uid>/cache-status
curl -b cookies.txt -X POST http://localhost:8043/api/studies/<uid>/warm    # 202; poll until "hot"
curl -b cookies.txt -X POST http://localhost:8043/api/studies/<uid>/evict

# Series-level (the cache-state unit is the series)
curl -b cookies.txt      http://localhost:8043/api/series/<series-uid>/cache-status
curl -b cookies.txt -X POST http://localhost:8043/api/series/<series-uid>/warm
curl -b cookies.txt -X POST http://localhost:8043/api/series/<series-uid>/evict
```

### Archiving, sizing, cleanup, health (all dry-run by default)

```bash
# Compress series to *.tar.zst (dry-run default; --execute writes + records paths)
python scripts/cold_storage/archive_all_series.py --dry-run
python scripts/cold_storage/archive_all_series.py --workers 4 --execute

# Backfill per-series/study storage sizes (compressed/decompressed MB)
python scripts/cold_storage/backfill_storage_sizes.py            # everything missing
python scripts/cold_storage/backfill_storage_sizes.py --label my_batch --workers 4

# Triage series with loose files but no archive (compression failures)
python scripts/cold_storage/list_unarchived_series.py --count
python scripts/cold_storage/list_unarchived_series.py --patient <patient-id>

# Delete loose DICOMs that are safe to remove (archive exists + Orthanc indexed)
python scripts/cold_storage/cleanup_loose_dicoms.py                  # dry-run
python scripts/cold_storage/cleanup_loose_dicoms.py --execute

# Verify (and optionally repair) archive integrity
python scripts/cold_storage/verify_and_repair_archives.py

# Cold-storage health probe (stuck warming, orphan dirs, disk free)
python scripts/cold_storage/cold_storage_health.py
```

### DICOM → NIfTI (on demand)

```bash
python scripts/dicom/dicom_to_nifti.py --dir /path/to/DICOM
python scripts/dicom/dicom_to_nifti.py --archive /path/to/DICOM.tar.zst --out /tmp/x.nii.gz
python scripts/dicom/dicom_to_nifti.py --series-uid <uid> --warm-if-cold
```

### Repair the Orthanc index

Repair stale Orthanc index entries (duplicate-path rot that makes OHIF panes blank):

```bash
# Report only (default)
python scripts/cold_storage/prune_stale_index_paths.py --patient <patient-id>
python scripts/cold_storage/prune_stale_index_paths.py --json        # all patients

# Apply (briefly stops Orthanc, backs up the index DB, restarts)
python scripts/cold_storage/prune_stale_index_paths.py --patient <patient-id> --execute --yes
python scripts/cold_storage/prune_stale_index_paths.py --execute      # global
```

Backfill series that are in `image_series` but missing from Orthanc's index (e.g.
after an indexing failure or a scan truncated by an Orthanc restart). Uses the
patched indexer's `POST /indexer/scan` endpoint and registers in **bounded
passes** (default ≤350 series / ≤40k instances per pass, 120 s settle — one huge
uninterrupted scan can OOM Orthanc):

```bash
python scripts/cold_storage/reindex_missing_series.py                            # dry-run, everything
python scripts/cold_storage/reindex_missing_series.py --label my_batch           # dry-run, one label
python scripts/cold_storage/reindex_missing_series.py --label my_batch --limit 400 --execute  # pilot
python scripts/cold_storage/reindex_missing_series.py --label my_batch --execute             # full label
python scripts/cold_storage/reindex_missing_series.py --exclude-label huge_batch --execute   # skip a label
python scripts/cold_storage/reindex_missing_series.py --pass-instances 20000 --execute       # gentler passes
# verify: python scripts/data_integrity/reconcile.py   (in_db_not_in_orthanc should drop)
```

Register an explicit list of series directly (library CLI):

```bash
python scripts/cold_storage/scoped_index.py --series <suid1,suid2> [--granularity series|study]
```

---

## Deleting studies / series

Complete removal across all three layers (Orthanc index + DB + on-disk files +
indexer purge). Full runbook: [`deleting_studies.md`](deleting_studies.md).
Dry-run by default; `--execute` needs a typed `yes` (no sudo — the service user
owns the storage roots). Annotations are discarded to history, not migrated.

```bash
# Review a patient's null/empty-description studies (a common faulty-upload sign)
python scripts/admin/delete_study.py --patient <patient-id> --null-description

# Dry-run, then execute (complete removal) for one or more studies
python scripts/admin/delete_study.py --study <UID>
python scripts/admin/delete_study.py --study <UID> --execute

# Maintenance sweep: on-disk study dirs with no image_study row
python scripts/admin/delete_study.py --purge-orphan-files --execute
```

Admins can also delete from the Studies/Series tables via the **trash-icon**
button — same complete removal, behind a confirmation modal.

---

## Backups

PostgreSQL + Orthanc-storage backups run nightly — via systemd timers on Linux
(`pg-backup-{stanford-stroke,orthanc,freshness}`, `orthanc-storage-backup`), or
via launchd on macOS (`com.ssc.pg-backup-*`, `com.ssc.orthanc-storage-backup`).
Strategy and rationale in [`backup_strategy.md`](backup_strategy.md). Recovery
procedure in [`restore_runbook.md`](restore_runbook.md). The dump root is
`config.toml` `[backup].backup_root`.

```bash
# On-demand backup (also runs nightly)
./scripts/backup/backup_pg_db.sh stanford-stroke
./scripts/backup/backup_pg_db.sh orthanc_db

# Inspect latest dumps (BR = the [backup].backup_root from config.toml)
BR=$(python3 -c "import tomllib;print(tomllib.load(open('config.toml','rb'))['backup']['backup_root'])")
ls -lh "$BR/stanford-stroke/" "$BR/orthanc_db/"

# Verify checksums
( cd "$BR/stanford-stroke" && sha256sum -c latest.dump.sha256 )

# Check schedule / tail the most recent run — Linux (systemd)
systemctl list-timers 'pg-backup-*'
sudo journalctl -u pg-backup-stanford-stroke.service -e
#   macOS: sudo launchctl list | grep com.ssc.pg-backup
#          tail -n 50 ~/Library/Logs/com.ssc.pg-backup-stanford-stroke.err

# Run the freshness monitor manually
./scripts/backup/check_backup_freshness.sh    # exit 0 = fresh, 2 = stale or missing
```

The cold-archive mirror (Tier 2) is implemented but **dormant** — see
`backup_strategy.md` §4 for the production cutover steps.

---

## Cutting a release

Versioning is deliberately minimal: a `vX.Y` git tag plus a 2–3 line entry in
the root `CHANGELOG.md`. Cut one when a change is changelog-worthy (a DB
migration, a user-visible change, a state you'd want to roll back to, or an
accumulation of small fixes). Bump `X` only for scary upgrades (irreversible
migration, storage-mode change); otherwise bump `Y`.

```bash
# 1. Add a 2-3 line entry to the root CHANGELOG.md (note any Alembic migration).
# 2. Tag and push.
git tag vX.Y
git push --tags
```

That's the whole procedure — no other tooling. `git describe --tags` may show
production a few commits past the last tag (e.g. `v1.2-3-g<sha>`); that is
expected. (`CHANGELOG.md` is created at the v1.0 audit→main merge.)
