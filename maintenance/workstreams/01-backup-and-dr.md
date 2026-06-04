# Workstream 01 — Backup and disaster recovery

**Status:** `done` (uncommitted — 2026-04-15)
**Priority:** `P0`
**Size:** `S/M` (≈ 3–5 days)
**Owner:** _(unassigned)_
**Dependencies:** none

---

## 1. Context

The PACS stack has **no documented backup or disaster-recovery strategy**.
Both PostgreSQL databases (`orthanc_db` and `stanford-stroke`) have no WAL
archiving, no snapshot schedule, and no tested restore procedure. Cold
archives at `/DATA2/pacs_imaging_data_compressed/` are a single copy on
one disk.

**Current deployment posture (as of 2026-04-15):** the system is in
development mode. DICOM / cold-archive data loss is **not** considered
fatal — re-ingesting from source is acceptable. **SQL data loss (annotations,
users, preferences, label definitions) IS fatal** — this data is authored in
the web app and cannot be reconstructed.

This workstream therefore has two tiers:

- **Tier 1 — active (enable in dev):** PostgreSQL backups for both logical
  DBs, freshness monitoring, tested restore runbook.
- **Tier 2 — dormant (implement but keep disabled):** cold-archive
  replication. The script and systemd timer exist in the repo and are
  production-ready, but the timer is **not** enabled on the dev host.
  When the system transitions to production, enabling the timer is a
  single `systemctl enable --now` command — no re-implementation required.

See `AUDIT_FINDINGS.md` §3.2 and §4.6.

---

## 2. Scope

**In scope — Tier 1 (enable in dev):**
- Automated, scheduled backups of `orthanc_db` and `stanford-stroke` (pg_dump
  or `pg_basebackup` + WAL).
- A documented, tested restore procedure in
  `stanford-stroke-pacs/documentation/operations/`.
- RTO/RPO targets documented.
- Monitoring for backup freshness (age-based alert) — **active** for
  PostgreSQL backups.

**In scope — Tier 2 (implement but keep disabled):**
- A cold-archive replication script (e.g. `rsync -a --delete` or
  borg/restic) and a systemd timer unit. Both committed to the repo.
  Timer stays **disabled** on the dev host. Production checklist will
  call out `systemctl enable --now cold-archive-mirror.timer` as the
  activation step.

**Out of scope:**
- Enabling the cold-archive mirror on the dev host.
- Backing up `/DATA2/pacs_imaging_data/` (hot cache; reconstructible from cold
  archives).
- High-availability / live replication for PostgreSQL (future work).
- Backing up the Orthanc container filesystem state (stateless by design).

---

## 3. Findings

- **F-01.1** — Cold archives are single-copy.
  - Evidence: `documentation/cold_storage/design.md`, `config.toml`
    `cold_archive_root = /DATA2/pacs_imaging_data_compressed`.
  - Impact in dev: acceptable — re-ingest is possible.
  - Impact in production: permanent loss of ~13,801 DICOM series. Tier 2
    addresses this; activation deferred to production cutover.
- **F-01.2** — No PostgreSQL backup documented.
  - Evidence: no references to `pg_dump`, `pg_basebackup`, `wal-g`, or
    `pgbackrest` anywhere in the repo; no cron/systemd timer units.
  - Impact: DB corruption or accidental `DROP` destroys annotations,
    users, preferences, and label definitions — **fatal** even in dev.
    Tier 1 addresses this; active on the dev host.
- **F-01.3** — No restore runbook.
  - Evidence: `documentation/operations/commands.md` covers day-2 ops but not
    recovery.
  - Impact: under pressure, there is no playbook — time-to-recover is
    unbounded.
- **F-01.4** — No top-level `README.md` means DR posture is invisible to new
  maintainers.
  - Evidence: no `README.md` at repo root or `stanford-stroke-pacs/` root.

---

## 4. Tasks

### Tier 1 — PostgreSQL backups (active on dev)

- [ ] **T1** — Choose backup tooling. Recommended: `pg_basebackup` + WAL
  archiving to a second disk (e.g. `/DATA3/pg_backups/`) using a simple
  `archive_command` in `postgresql.conf`. Alternative: daily `pg_dump -Fc`
  if WAL archiving is too heavy. Document the decision in
  `documentation/operations/backup_strategy.md` (new file).
- [ ] **T2** — Provision a backup destination. Either a second local disk or
  an NFS/SSH target. Confirm write capacity ≥ 2× the DB size plus 30 days
  of WAL.
- [ ] **T3** — Configure PostgreSQL `archive_mode = on` and an
  `archive_command` that copies WAL segments to the backup destination.
  Restart PostgreSQL. Verify WAL is arriving.
- [ ] **T4** — Install a systemd timer that runs nightly `pg_basebackup` (or
  `pg_dump`) against both logical DBs. Use a distinct unit per DB so failures
  are isolated. Store unit files in
  `stanford-stroke-pacs/systemd/pg-backup-orthanc.{service,timer}` and
  `pg-backup-stanford-stroke.{service,timer}` (new files). **Enable the
  timers on the dev host.**
- [ ] **T6** — Add a freshness monitor. A simple shell script that checks the
  mtime of the latest backup file; if older than 36 h, exits nonzero. Wire
  into a systemd unit with `OnFailure=` and a (future) alerting hook.
  Scope: PostgreSQL backups only in dev; the cold-archive check is
  implemented but gated behind a `--include-cold-archive` flag that the
  dev-host invocation does not pass.

### Tier 2 — cold-archive replication (implement, keep disabled)

- [ ] **T5** — Implement cold-archive replication — **commit the code but
  do not enable the timer on dev**. Options:
  - **Simplest:** nightly `rsync -a --delete
    /DATA2/pacs_imaging_data_compressed/ /DATA3/pacs_imaging_data_compressed_mirror/`
    via a systemd timer.
  - **Better (for production):** `borgbackup` or `restic` to a remote
    host for off-site copy.
  Commit the script + systemd unit files. Ship the timer with
  `[Install] WantedBy=` set but do not `systemctl enable` it. Document
  the activation command in the production-cutover section of the
  backup-strategy doc.

### Documentation and verification (both tiers)

- [ ] **T7** — Write `documentation/operations/restore_runbook.md` (new file)
  covering:
  - How to restore `stanford-stroke` from basebackup + WAL. **(Tier 1 —
    must be tested on dev.)**
  - How to restore `orthanc_db`, including re-indexing steps if needed.
    **(Tier 1 — must be tested on dev.)**
  - How to restore cold archives from the mirror. **(Tier 2 — documented
    but not rehearsed on dev; flagged for production cutover rehearsal.)**
  - How to validate a restore without touching production.
- [ ] **T8** — Document **RTO/RPO** targets in the new backup-strategy doc.
  - Dev / current: SQL RPO = 24 h, RTO = 4 h. Cold archives = "re-ingest
    from source."
  - Production (future): SQL RPO = 24 h, RTO = 4 h. Cold archives RPO =
    24 h, RTO = TBD (depends on chosen offsite target).
- [ ] **T9** — **Perform a dry-run restore of Tier 1** on a scratch PostgreSQL
  instance. Restore both DBs from the most recent backup and verify row
  counts against production on a sample table (e.g. `image_series`,
  `annotations`). This is the **acceptance gate** for Tier 1 — if the
  restore fails, the backup is worthless. Tier 2 dry-run is deferred to
  production cutover.
- [ ] **T10** — Link the new docs from
  `documentation/context.md` and `documentation/operations/commands.md`.
- [ ] **T11** — Add a **production cutover checklist** to
  `backup_strategy.md` with the explicit Tier 2 activation steps:
  1. Provision the mirror destination.
  2. Run a first manual sync.
  3. `systemctl enable --now cold-archive-mirror.timer`.
  4. Re-run restore rehearsal including cold archives.
  5. Update RTO/RPO in this doc to production values.

---

## 5. Acceptance criteria

### Tier 1 (active on dev)

- [ ] Nightly basebackups (or dumps) run successfully for three consecutive
  nights with no errors in `journalctl -u pg-backup-*`.
- [ ] Dry-run restore (T9) successfully recreates both DBs on a scratch
  instance with matching row counts.
- [ ] Freshness monitor (PostgreSQL-only mode) exits nonzero when a backup
  is artificially aged past the threshold.

### Tier 2 (committed, inactive)

- [ ] Cold-archive mirror script and systemd unit files exist in the repo
  with clear "disabled on dev" comments at the top of the unit.
- [ ] `systemctl list-unit-files cold-archive-mirror.timer` shows
  `disabled` on the dev host.
- [ ] Manually invoking the mirror script in `--dry-run` mode on dev
  succeeds (proves the script works; does not actually copy).

### Documentation (both tiers)

- [ ] `documentation/operations/restore_runbook.md` exists and has been read
  aloud by someone other than the author.
- [ ] `backup_strategy.md` includes the production-cutover checklist (T11).
- [ ] Docs clearly distinguish dev vs. production posture.

---

## 6. Verification

```bash
# Tier 1: pg-backup timers are active
systemctl list-timers 'pg-backup-*'   # expect active

# Tier 2: cold-archive timer is present but DISABLED on dev
systemctl list-unit-files cold-archive-mirror.timer   # expect 'disabled'
systemctl is-active cold-archive-mirror.timer         # expect 'inactive'

# Latest pg backup is recent
ls -lh /DATA3/pg_backups/orthanc_db/ | tail -5
ls -lh /DATA3/pg_backups/stanford-stroke/ | tail -5

# WAL is being archived (look for .backup and WAL files)
ls /DATA3/pg_wal_archive/ | head -20

# Tier 2 script works in dry-run mode (does not actually copy on dev)
stanford-stroke-pacs/scripts/cold_storage/mirror_cold_archive.sh --dry-run

# Freshness monitor (PostgreSQL-only mode)
/usr/local/bin/check_backup_freshness.sh
echo $?  # should be 0
```

Dry-run restore — on a scratch host or in a container:

```bash
# Restore stanford-stroke to a test DB
createdb stanford_stroke_restore_test
pg_restore -d stanford_stroke_restore_test /DATA3/pg_backups/stanford-stroke/latest.dump

# Compare row counts
psql -d stanford-stroke -c "SELECT count(*) FROM image_series;"
psql -d stanford_stroke_restore_test -c "SELECT count(*) FROM image_series;"
```

---

## 7. Rollback

Pure-ops workstream. To roll back:

```bash
sudo systemctl disable --now pg-backup-orthanc.timer pg-backup-stanford-stroke.timer
# cold-archive-mirror.timer is not enabled on dev — nothing to disable
# Optionally set archive_mode = off in postgresql.conf and restart postgres
```

The backups themselves can stay on disk (they do no harm).

---

## 8. Files touched

- `stanford-stroke-pacs/systemd/pg-backup-orthanc.service` (new)
- `stanford-stroke-pacs/systemd/pg-backup-orthanc.timer` (new)
- `stanford-stroke-pacs/systemd/pg-backup-stanford-stroke.service` (new)
- `stanford-stroke-pacs/systemd/pg-backup-stanford-stroke.timer` (new)
- `stanford-stroke-pacs/systemd/pg-backup-freshness.service` (new — added
  during execution; T6 freshness-monitor systemd wiring)
- `stanford-stroke-pacs/systemd/pg-backup-freshness.timer` (new — added
  during execution; T6 freshness-monitor systemd wiring)
- `stanford-stroke-pacs/systemd/cold-archive-mirror.service` (new, **not
  enabled on dev**)
- `stanford-stroke-pacs/systemd/cold-archive-mirror.timer` (new, **not
  enabled on dev**)
- `stanford-stroke-pacs/scripts/cold_storage/mirror_cold_archive.sh` (new, supports
  `--dry-run`; called by the systemd service above)
- `stanford-stroke-pacs/scripts/backup/check_backup_freshness.sh` (new)
- `stanford-stroke-pacs/scripts/backup/backup_pg_db.sh` (new)
- `stanford-stroke-pacs/documentation/operations/backup_strategy.md` (new)
- `stanford-stroke-pacs/documentation/operations/restore_runbook.md` (new)
- `stanford-stroke-pacs/documentation/context.md` (edit — add links)
- `stanford-stroke-pacs/documentation/operations/commands.md` (edit — add links)

PostgreSQL configuration (`postgresql.conf`) is on the host, not the repo —
document the exact diff required in `backup_strategy.md`.

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Backup disk fills up | med | med | Retention policy in backup script; alerts on disk usage |
| WAL archiving impacts write latency | low | low | Measure before/after; fall back to nightly `pg_dump` |
| Restore tested in isolation but fails in real incident | med | high | Quarterly restore drill; keep the runbook current |
| Tier 2 code bit-rots while disabled | high | low | Monthly `--dry-run` invocation in dev; revisit at production cutover |
| Production cutover forgets to enable Tier 2 | med | high | T11 checklist; put it in the cutover runbook and require sign-off |
| Offsite copy (when eventually enabled) involves PHI-adjacent data | med | high | If cold archives leave the host, ensure encryption at rest (borg/restic both support this); revisit at cutover |

---

## 10. Notes

- **Dev-mode posture (2026-04-15):** DICOM / cold-archive loss is
  acceptable (re-ingest from source). SQL data loss is fatal. Tier 1 is
  live; Tier 2 is committed but disabled. This posture should be
  reconsidered every time the deployment stage changes — revisit this
  workstream before any production cutover.
- Anonymization status of DICOMs affects where archives can legally be copied.
  Check whether `image_integration_protocol.anonymize_files` is enabled in
  production before configuring an offsite mirror.
- If PostgreSQL is a systemd-managed package install (not containerized),
  `archive_command` edits are permanent across package upgrades as long as
  `postgresql.conf` lives outside the package.
- Consider `pgbackrest` for a more production-grade solution if the stack
  survives and grows. `pg_basebackup` is the minimum-viable baseline.
