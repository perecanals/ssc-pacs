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
./check_status.sh

# Full teardown (removes container, volume, DB) — DESTRUCTIVE
./teardown.sh
```

### Companion (native systemd service)

```bash
# Start / stop / restart
sudo systemctl start ssc-companion
sudo systemctl stop ssc-companion
sudo systemctl restart ssc-companion

# Check status
sudo systemctl status ssc-companion

# View live logs
sudo journalctl -u ssc-companion -f

# Enable auto-start on boot (one-time setup)
sudo cp ssc-companion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ssc-companion

# Rebuild frontend after code changes
cd companion && npm run build
sudo systemctl restart ssc-companion

# Run manually (development, with auto-reload)
cd companion && uvicorn app:app --port 8043 --reload
```

---

## User management

Users are stored in the `users` PostgreSQL table (bcrypt hashes) and
in `orthanc_users.json` (plaintext, required by Orthanc). Both are managed
atomically by `manage_users.py`. **Never edit `orthanc_users.json` by hand.**

```bash
# List all users
python manage_users.py list

# Add a regular user (prompts for password with hidden input + confirmation)
python manage_users.py add alice

# Add an admin user
python manage_users.py add bob --admin

# Change a user's password
python manage_users.py passwd alice

# Remove a user
python manage_users.py remove alice
```

After any change, restart Orthanc to pick up the updated `orthanc_users.json`:

```bash
docker restart ssc-orthanc
```

If the modified user matches `ORTHANC_ADMIN_USER` in `.env`, the script also
updates `ORTHANC_ADMIN_PASSWORD` there so the Companion's service-to-service
calls stay in sync. After changing the admin password, also restart the
Companion:

```bash
sudo systemctl restart ssc-companion
```

---

## SSH tunnel (run from local machine)

```bash
# Open tunnel (includes companion app on 8043)
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
- **Companion (landing + app):** http://localhost:8043/ and http://localhost:8043/app/
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

# Companion logs (systemd)
sudo journalctl -u ssc-companion -f
sudo journalctl -u ssc-companion --since "5 min ago"
```

---

## Indexing and enrichment

```bash
# Check indexing progress
curl -s -u admin:<password> http://localhost:8042/statistics | python3 -m json.tool

# Verify indexed series vs SQL table (compares image_series vs Orthanc)
python verify_indexing.py

# Enrich studies/series with patient_id, seriesdescription (re-run after new indexing)
python enrich_orthanc.py
```

---

## Labels (OE2)

Labels can be managed through the OE2 web UI or via the REST API.

```bash
# Pre-populate labels from source DB (study_type + modality). Idempotent, safe to re-run.
python label_studies.py

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

After running `label_studies.py`, typical labels include:

- **Study type:** `BASAL`, `THROMBECTOMY`, `FOLLOW_UP`, `OTHER`
- **Modality:** `CT`, `MR`, etc.

Users can add custom labels through the OE2 UI. All labels are shared across users.

---

## Companion API examples

Multi-level annotations live in the `stanford-stroke` database; see [`../reference/data_stores.md`](../reference/data_stores.md).

```bash
# Rebuild frontend and restart the companion service
cd companion && npm run build
sudo systemctl restart ssc-companion

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
sudo ./remove_label.sh "My Label Name"
```

### Removing the companion app

1. Stop and disable the service: `sudo systemctl disable --now ssc-companion`
2. Remove the unit file: `sudo rm /etc/systemd/system/ssc-companion.service`
3. Delete the `companion/` folder and `ssc-companion.service`
4. (Optional) drop companion-owned tables in `stanford-stroke` if you no longer need them

Orthanc is unaffected.

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
./scripts/backup_pg_db.sh stanford-stroke
./scripts/backup_pg_db.sh orthanc_db

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
./scripts/check_backup_freshness.sh    # exit 0 = fresh, 2 = stale or missing
```

The cold-archive mirror (Tier 2) is implemented but **dormant** on the dev
host — see `backup_strategy.md` §4 for the production cutover steps.
