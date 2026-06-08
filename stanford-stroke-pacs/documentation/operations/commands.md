# Operations commands (cheat sheet)

**Purpose:** Day-2 commands and quick API examples. For first-time deploy see [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md). For runtime/config context see [`../reference/runtime_and_config.md`](../reference/runtime_and_config.md).

Run commands from the **repo root** unless noted.

---

## Server control

### Orthanc (Docker)

```bash
# Start Orthanc
docker compose up -d

# Stop Orthanc
docker compose down

# Restart Orthanc
docker compose restart

# Full status check (Docker, API, plugins, stats)
./scripts/orthanc/check_status.sh

# Full teardown (removes container, volume, DB) — DESTRUCTIVE
./scripts/admin/teardown.sh
```

### Web App (native systemd service)

```bash
# Start / stop / restart
sudo systemctl start ssc-web-app
sudo systemctl stop ssc-web-app
sudo systemctl restart ssc-web-app

# Check status
sudo systemctl status ssc-web-app

# View live logs
sudo journalctl -u ssc-web-app -f

# Enable auto-start on boot (one-time setup)
sudo cp systemd/ssc-web-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ssc-web-app

# Rebuild frontend after code changes
cd web-app && npm run build
sudo systemctl restart ssc-web-app

# Run manually (development, with auto-reload)
cd web-app && uvicorn app:app --port 8043 --reload
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

# Add a regular user (DB only) — admin types a temporary password
python scripts/admin/manage_users.py add alice

# Add an admin user (DB + orthanc_users.json)
python scripts/admin/manage_users.py add bob --admin

# Reset a user's password (admin-driven; user is forced to change it again)
python scripts/admin/manage_users.py passwd alice

# Remove a user
python scripts/admin/manage_users.py remove alice
```

`add` and `passwd` both set the user's `must_change_password` flag to TRUE. On
their next sign-in the Navigator UI redirects them to `/change-password` and
the API blocks every other endpoint with `403 password_change_required` until
they pick a new password. There is no self-service password reset — a forgotten
password requires an admin to run `passwd` and share a fresh temporary one
out-of-band.

Adding, removing, or changing the password of a **non-admin** user only touches
PostgreSQL — no service restart is needed. For **admin** users the script also
updates `orthanc_users.json`; restart Orthanc to pick it up:

```bash
docker restart ssc-orthanc
```

### Rotating the Orthanc service account

The service account is the credential Web App uses to proxy to Orthanc and
that host-local scripts use for direct Orthanc access (`ORTHANC_ADMIN_USER`
in `.env`). Rotate it with:

```bash
python scripts/admin/manage_users.py rotate-service-account
```

This rewrites `ORTHANC_ADMIN_PASSWORD` in `.env` and the matching entry in
`orthanc_users.json` atomically. Then restart both services:

```bash
docker restart ssc-orthanc
sudo systemctl restart ssc-web-app
```

---

## SSH tunnel (run from local machine)

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

# Orthanc container logs (follow)
docker compose logs -f orthanc

# Recent Orthanc logs only
docker logs --since 5m ssc-orthanc

# Web App logs (systemd)
sudo journalctl -u ssc-web-app -f
sudo journalctl -u ssc-web-app --since "5 min ago"
```

---

## Indexing and enrichment

```bash
# Check indexing progress
curl -s -u admin:<password> http://localhost:8042/statistics | python3 -m json.tool

# Two-DB reconciliation (image_series vs Orthanc index + disk checks)
python scripts/data_integrity/reconcile.py               # human-readable summary
python scripts/data_integrity/reconcile.py --json        # write JSON report
python scripts/data_integrity/reconcile.py --json --quiet # cron/timer mode

# Enrich studies/series with patient_id, seriesdescription (re-run after new indexing)
python scripts/orthanc/enrich_orthanc.py
```

---

## Labels (OE2)

Labels can be managed through the OE2 web UI or via the REST API.

```bash
# Pre-populate labels from source DB (study_type + modality). Idempotent, safe to re-run.
python scripts/orthanc/label_studies.py

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

### Pre-seeded labels

After running `scripts/orthanc/label_studies.py`, typical labels include:

- **Study type:** `BASAL`, `THROMBECTOMY`, `FOLLOW_UP`, `OTHER`
- **Modality:** `CT`, `MR`, etc.

Users can add custom labels through the OE2 UI. All labels are shared across users.

---

## Web App API examples

Multi-level annotations live in the `stanford-stroke` database; see [`../reference/data_stores.md`](../reference/data_stores.md).

```bash
# Rebuild frontend and restart the web app service
cd web-app && npm run build
sudo systemctl restart ssc-web-app

# List all annotation labels via the API
curl -s http://localhost:8043/api/labels | python3 -m json.tool

# Label summary with counts
curl -s http://localhost:8043/api/labels/summary | python3 -m json.tool

# Search series (optional filters: label, patient_id, modality, description, study_type)
curl -s 'http://localhost:8043/api/series?label=hemorrhagic&per_page=10' | python3 -m json.tool

# Add an annotation via API (writes use JWT in browser; for curl you typically need auth cookie)
curl -s -X POST http://localhost:8043/api/annotations \
  -H 'Content-Type: application/json' \
  -d '{"seriesinstanceuid":"1.2.3...", "studyinstanceuid":"1.2.3...", "label":"reviewed", "created_by":"alice"}'

# Remove an annotation by ID
curl -s -X DELETE http://localhost:8043/api/annotations/42

# Remove a label entirely (definition + annotation rows) — run from repo root
sudo ./scripts/admin/remove_label.py "My Label Name"

# Bulk-set a label's values from a CSV/Excel table (admin backdoor).
# Creates the label if it does not exist (interactive y/n unless --yes).
sudo bash scripts/admin/bulk_set_label_values.sh \
    --file /tmp/series_quality.csv --level series \
    --id-column seriesinstanceuid --value-column quality \
    --label series_quality --datatype select --options 'good,acceptable,poor' \
    --dry-run
```

### Removing the web app app

1. Stop and disable the service: `sudo systemctl disable --now ssc-web-app`
2. Remove the unit file: `sudo rm /etc/systemd/system/ssc-web-app.service`
3. Delete the `web-app/` folder and `systemd/ssc-web-app.service`
4. (Optional) drop web-app-owned tables in `stanford-stroke` if you no longer need them

Orthanc is unaffected.

---

## Backend module structure

The web app backend is split into focused modules under `web-app/`:

| Module | Purpose |
|--------|---------|
| `app.py` | Entry point: lifespan, middleware, router registration (~230 lines) |
| `db.py` | Single source of truth for `DB_CONFIG` and `ThreadedConnectionPool` |
| `auth.py` | JWT utilities (`create_jwt`, `decode_jwt`, `get_current_user`) |
| `orthanc_client.py` | Orthanc REST wrappers (`orthanc_lookup`, `orthanc_system_check`) |
| `common.py` | Shared SQL builders, annotation helpers, constants |
| `config.py` | Loads `config.toml` settings |
| `cache_manager.py` | Cold-storage warm/evict logic |
| `routes/*.py` | `APIRouter` submodules (auth, studies, annotations, labels, etc.) |

**Scripts** under `scripts/` import `DB_CONFIG` and `get_conn` from
`web-app/db.py` (via `sys.path` insertion). They no longer define their
own database config inline.

---

## Testing and code quality

Run from the **repo root** with `conda activate pacs`.

```bash
# One-time dev setup (Python deps, Node deps, pre-commit hooks)
make install-dev

# Run all tests (backend + frontend)
make test

# Backend only (pytest — needs local Postgres; creates scratch DB)
make test-backend

# Frontend only (vitest — no Postgres needed)
make test-frontend

# Lint (ruff on web-app/)
make lint
```

**CI:** GitHub Actions runs on every push to `main` and every PR
(`.github/workflows/ci.yml`). Required jobs: lint, backend-tests,
frontend-tests, frontend-build. The mypy job is advisory (non-blocking).

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

---

## Backups

PostgreSQL backups run nightly via systemd timers. Strategy and rationale in
[`backup_strategy.md`](backup_strategy.md). Recovery procedure in
[`restore_runbook.md`](restore_runbook.md).

```bash
# On-demand backup (also runs nightly via timer)
./scripts/backup/backup_pg_db.sh stanford-stroke
./scripts/backup/backup_pg_db.sh orthanc_db

# Inspect latest dumps
ls -lh /DATA2/pg_backups/stanford-stroke/ /DATA2/pg_backups/orthanc_db/

# Verify checksums
( cd /DATA2/pg_backups/stanford-stroke && sha256sum -c latest.dump.sha256 )

# Check timer schedule
systemctl list-timers 'pg-backup-*'

# Tail the most recent backup run
sudo journalctl -u pg-backup-stanford-stroke.service -e
sudo journalctl -u pg-backup-orthanc.service -e

# Run the freshness monitor manually
./scripts/backup/check_backup_freshness.sh    # exit 0 = fresh, 2 = stale or missing
```

The cold-archive mirror (Tier 2) is implemented but **dormant** on the dev
host — see `backup_strategy.md` §4 for the production cutover steps.
