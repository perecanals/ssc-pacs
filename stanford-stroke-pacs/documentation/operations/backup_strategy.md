# Backup strategy

**Status (2026-04-15):** Tier 1 active on the dev host. Tier 2 (cold-archive
mirror) implemented but **dormant** — activation is part of the production
cutover checklist below.

This document is the single source of truth for what is backed up, how, where,
and how to recover. Restore steps live in
[`restore_runbook.md`](restore_runbook.md).

---

## 1. What is and isn't backed up

| Data | Where | Backed up? | Why |
|---|---|---|---|
| `stanford-stroke` PostgreSQL DB | host PostgreSQL 16 | **Yes — Tier 1, daily** | Authored content (annotations, users, label defs, preferences) cannot be reconstructed |
| `orthanc_db` PostgreSQL DB | host PostgreSQL 16 | **Yes — Tier 1, daily** | Orthanc's index — rebuildable from disk but slow; cheap to back up |
| Cold DICOM archives `/DATA2/pacs_imaging_data_compressed/` | local disk | **No on dev**, mirror script implemented for production (Tier 2) | Dev: re-ingest from source is acceptable. Production: requires a destination |
| Hot cache `/DATA2/pacs_hot_cache/` and `/DATA2/pacs_imaging_data/` | local disk | No | Reconstructible from cold archives on demand |
| Orthanc storage volume (`…_ssc-orthanc-storage` at `/var/lib/orthanc/db`) | docker volume | **Yes — Tier 1, daily** | Holds OHIF-authored DICOM SR annotations (**no other copy**) + the Folder Indexer `indexer-plugin.db` (rebuild = full cold-archive decompression + reindex) |
| Orthanc container filesystem (rootfs) | docker | No | Stateless; rebuilt from `docker compose up` + the `ssc-orthanc:patched-indexer` image |
| Web App `dist/` build output | local | No | Reproducible via `npm run build` |
| `.env`, `config.toml`, `orthanc.json`, `orthanc_users.json` | repo / host | Out of scope here | Tracked separately (git for non-secrets, secret management for `.env`) |

---

## 2. RTO / RPO targets

| Target | Dev (current) | Production (future) |
|---|---|---|
| `stanford-stroke` RPO | 24 h | 24 h (or shorter with WAL — see §6) |
| `stanford-stroke` RTO | 4 h | 4 h |
| `orthanc_db` RPO | 24 h | 24 h |
| `orthanc_db` RTO | 4 h (or "rebuild from disk", which is hours) | 4 h |
| Orthanc storage volume RPO | 24 h | 24 h |
| Orthanc storage volume RTO | 4 h (or "decompress + reindex", which is hours) | 4 h |
| Cold archives RPO | n/a — re-ingest from source | 24 h |
| Cold archives RTO | n/a | TBD (depends on chosen offsite target) |

---

## 3. Tier 1 — PostgreSQL DBs + Orthanc storage volume (active on dev)

### Tooling

`pg_dump --format=custom --compress=6 --no-owner --no-privileges` per
database, run nightly by a systemd timer. Custom format (`-Fc`) is chosen
over `pg_basebackup` + WAL because:

- The dev RPO of 24 h does not justify WAL archiving complexity.
- Logical dumps are trivially restorable into any matching-or-newer PG
  major version (pg_restore is forward-compatible).
- No `postgresql.conf` change required, so the host PostgreSQL package
  upgrades safely without re-applying our changes.

`pg_basebackup` + WAL is the recommended **production upgrade** if RPO
needs to drop below 24 h. The migration is additive — keep the
nightly logical dumps as a portable safety net.

### Tooling version requirement

`pg_dump` from `postgresql-client-N` requires `N >= server_version`. The
host runs PostgreSQL **16.2**. Install client 16+:

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  | sudo gpg --dearmor -o /etc/apt/keyrings/pgdg.gpg
echo "deb [signed-by=/etc/apt/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt update
sudo apt install -y postgresql-client-16
```

When the server is upgraded, install the matching client major.

### Layout on disk

```
/DATA2/pg_backups/
├── orthanc_db/
│   ├── 20260415T024500Z.dump
│   ├── 20260415T024500Z.dump.sha256
│   ├── latest.dump  -> 20260415T024500Z.dump
│   └── latest.dump.sha256 -> ...
├── stanford-stroke/
│   ├── 20260415T024500Z.dump
│   ├── 20260415T024500Z.dump.sha256
│   ├── latest.dump  -> ...
│   └── latest.dump.sha256 -> ...
└── orthanc_storage/
    ├── 20260415T024500Z.tar.gz
    ├── 20260415T024500Z.tar.gz.sha256
    ├── latest.tar.gz  -> ...
    └── latest.tar.gz.sha256 -> ...
```

- One file per night per DB / per volume.
- `latest.dump` / `latest.tar.gz` symlinks always point at the newest archive.
- `.sha256` sidecar written immediately after each archive.
- Retention: archives older than `RETENTION_DAYS` (default 60) are deleted,
  but at least one is always kept.

### Orthanc storage volume — how the snapshot stays zero-downtime

`orthanc_db` (the index) is dumped by `pg_dump`, but the index points at file
payloads in the Orthanc storage Docker volume (`<project>_ssc-orthanc-storage`,
i.e. `stanford-stroke-pacs_ssc-orthanc-storage` here — Compose prefixes the
`docker-compose.yml` `volumes:` key with the project name) — including the
**only copy** of OHIF-authored SR annotations. `backup_orthanc_storage.sh`
captures that volume **without pausing Orthanc**:

- a throwaway helper container mounts the volume **read-only** and runs
  `scripts/backup/orthanc_storage_snapshot.py` (pure Python stdlib);
- the helper copies the live SQLite trio (`indexer-plugin.db{,-wal,-shm}`) into
  its own ephemeral space, lets SQLite WAL-recover + `checkpoint(TRUNCATE)` it in
  isolation, runs `PRAGMA integrity_check`, and streams a **gzip tar** of all the
  immutable volume files **plus** that consistent DB to stdout;
- the host redirects the stream to `<ts>.tar.gz` and does the same
  sha256 / `latest` symlink / retention bookkeeping as the pg dumps.

The production volume is never written. The irreplaceable SR DICOMs are always
captured cleanly; the only residual risk — a torn copy of the live ~1 GB DB → a
one-off degraded snapshot (helper exits 5, logged) — affects only the
*rebuildable* index and self-heals on the next run. This is the same
cross-snapshot non-atomicity already accepted between the separate pg-dump and
volume-backup timers; no `docker pause` is used.

### Files

| Path | Role |
|---|---|
| `scripts/backup/backup_pg_db.sh` | dump one DB, write sha256, rotate retention |
| `scripts/backup/backup_orthanc_storage.sh` | snapshot the storage volume via a `:ro` helper container, write sha256, rotate retention |
| `scripts/backup/orthanc_storage_snapshot.py` | in-container helper: consistent SQLite snapshot + gzip-tar stream to stdout |
| `scripts/backup/check_backup_freshness.sh` | exit nonzero if any latest dump/archive is older than `MAX_AGE_HOURS` (default 36) |
| `systemd/pg-backup-stanford-stroke.{service,timer}` | nightly dump of `stanford-stroke` (02:15 + jitter) |
| `systemd/pg-backup-orthanc.{service,timer}` | nightly dump of `orthanc_db` (02:30 + jitter) |
| `systemd/orthanc-storage-backup.{service,timer}` | nightly snapshot of the Orthanc storage volume (02:45 + jitter) |
| `systemd/pg-backup-freshness.{service,timer}` | hourly freshness check |

The backup script reads connection details from the same `.env` the
Web App uses (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`).
Override the env file with `BACKUP_ENV_FILE=...` if needed.

### Installation on the dev host

```bash
cd /home/perecanals/pacs/stanford-stroke-pacs

# Stage units in /etc
sudo cp systemd/pg-backup-stanford-stroke.service \
        systemd/pg-backup-stanford-stroke.timer \
        systemd/pg-backup-orthanc.service \
        systemd/pg-backup-orthanc.timer \
        systemd/orthanc-storage-backup.service \
        systemd/orthanc-storage-backup.timer \
        systemd/pg-backup-freshness.service \
        systemd/pg-backup-freshness.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start the timers
sudo systemctl enable --now \
    pg-backup-stanford-stroke.timer \
    pg-backup-orthanc.timer \
    orthanc-storage-backup.timer \
    pg-backup-freshness.timer

# Verify
systemctl list-timers 'pg-backup-*' 'orthanc-storage-backup*'
```

### Verification

```bash
# Latest archives on disk
ls -lh /DATA2/pg_backups/orthanc_db/ /DATA2/pg_backups/stanford-stroke/ /DATA2/pg_backups/orthanc_storage/

# Run the freshness monitor manually
/home/perecanals/pacs/stanford-stroke-pacs/scripts/backup/check_backup_freshness.sh
echo "exit=$?"   # 0 = fresh, 2 = stale or missing

# Run a backup on demand (any time)
/home/perecanals/pacs/stanford-stroke-pacs/scripts/backup/backup_pg_db.sh stanford-stroke
/home/perecanals/pacs/stanford-stroke-pacs/scripts/backup/backup_pg_db.sh orthanc_db
/home/perecanals/pacs/stanford-stroke-pacs/scripts/backup/backup_orthanc_storage.sh
```

### Monitoring / alerting (TODO)

The freshness check runs hourly. To page on failure, set
`OnFailure=` on `pg-backup-freshness.service` once an alerting webhook
exists (e.g. a `notify-on-failure@.service` instance). Until then,
nonzero exits show up in `journalctl -u pg-backup-freshness`.

---

## 4. Tier 2 — cold-archive mirror (DORMANT on dev)

The script and systemd units are committed but the timer is **not**
enabled on the dev host. There is no `/DATA3/` on this machine and DICOM
loss is recoverable via re-ingestion.

### Files

| Path | Role |
|---|---|
| `scripts/cold_storage/mirror_cold_archive.sh` | `rsync -a --delete` from `SOURCE_DIR` to `COLD_MIRROR_DEST` (no-op if `COLD_MIRROR_DEST` unset) |
| `systemd/cold-archive-mirror.service` | reads `/etc/default/pacs-cold-mirror`, runs the script |
| `systemd/cold-archive-mirror.timer` | nightly at 03:30 + jitter, **not enabled by default** |

### Production cutover checklist

1. Provision a destination — local disk (`/DATA3/cold_mirror`), NFS mount,
   or a borg/restic repository on a remote host.
2. Re-evaluate the rsync target choice if PHI is at stake. If
   `image_integration_protocol.anonymize_files` is **off**, the archives
   contain identifiable data and must not leave the host without
   encryption at rest (borg/restic both support this). See
   `documentation/reference/image_integration_protocol.md`.
3. Create `/etc/default/pacs-cold-mirror` (mode 0644, root-owned):

   ```ini
   SOURCE_DIR=/DATA2/pacs_imaging_data_compressed
   COLD_MIRROR_DEST=/path/to/mirror
   # Optional rsync tuning, e.g.:
   # RSYNC_EXTRA_ARGS=--bwlimit=50000
   ```

4. First manual sync (sanity check, may take hours):

   ```bash
   sudo systemctl start cold-archive-mirror.service
   sudo journalctl -u cold-archive-mirror.service -e
   ```

5. Enable the timer:

   ```bash
   sudo cp systemd/cold-archive-mirror.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now cold-archive-mirror.timer
   ```

6. Switch the freshness monitor to include the cold mirror:

   ```bash
   sudo systemctl edit pg-backup-freshness.service
   ```
   Add:
   ```ini
   [Service]
   ExecStart=
   ExecStart=/home/perecanals/pacs/stanford-stroke-pacs/scripts/backup/check_backup_freshness.sh --include-cold-archive
   EnvironmentFile=/etc/default/pacs-cold-mirror
   ```

7. Rehearse the cold-archive restore (see
   [`restore_runbook.md`](restore_runbook.md) §3).

8. Update the RTO/RPO row for cold archives in this doc once the
   destination is finalized.

---

## 5. Restore

See [`restore_runbook.md`](restore_runbook.md). The Tier 1 dry-run
restore was rehearsed on **2026-04-15** as the WS01 acceptance gate;
results are in that file.

---

## 6. Future upgrades

- **PITR:** add `archive_mode = on` + `archive_command` to ship WAL to
  `/DATA2/pg_wal_archive/` (or remote), then `pg_basebackup` weekly.
  Recovery becomes "restore basebackup, replay WAL up to a chosen
  timestamp." Drops `stanford-stroke` RPO toward seconds. Cost: a host
  PostgreSQL config change + monitoring WAL disk usage.
- **Offsite logical dumps:** rsync `/DATA2/pg_backups/` to a second
  machine on a daily timer. Trivial once a second host exists.
- **`pgbackrest`:** consider for production-grade differential basebackups
  with built-in retention and parallel restore. Heavier than the current
  setup but more complete.

---

## 7. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Backup disk fills up | med | med | `RETENTION_DAYS` rotation in script; monitor `/DATA2` usage separately |
| pg_dump version drifts behind server after PG upgrade | low | high | Re-install `postgresql-client-N` matching new server major |
| Restore tested in isolation but fails in real incident | med | high | Quarterly restore drill; keep `restore_runbook.md` current |
| `.env` rotates and the backup script silently uses stale creds | low | high | Backup failure surfaces in `pg-backup-freshness` within 36h |
| Dump lands on the same physical disk as the live DB | med | med | Tier 1 is single-disk on dev by design; production should mount `/DATA2` separate from PG data |
