# Remote backups over SSH

**Status:** implemented, opt-in, not enabled by installation. Local Tier 1
backups continue unchanged. The legacy `cold-archive-mirror` timer remains
disabled when using this alternative. Overall policy is in
[`backup_strategy.md`](backup_strategy.md).

`scripts/backup/remote_backup.py` uses restic to create encrypted, versioned
repositories on a second host over SSH/SFTP. Only the source needs restic
(tested with 0.19.1). The destination needs SSH/SFTP and Python 3 for mount and
capacity checks; Linux and macOS destinations are supported.

## Configuration authority

The deployment's `stanford-stroke-pacs/config.toml` is the single source of
truth for non-secret backup settings:

- `[storage].cold_archive_root`: imaging source, shared with ingestion.
- `[backup].backup_root`: local backup source, shared by producers and remote uploads.
- `[backup].max_age_hours`: one freshness threshold for local and remote copies.
- `[backup].retention_days`: retention of local dated backup files.
- `[backup].cold_mirror_dest` / `cold_mirror_rsync_args`: legacy mirror settings,
  dormant when using restic. Newly rendered units do not read the former
  `/etc/default/pacs-cold-mirror` file.
- `[backup.schedules]`: calendars and jitter for all Linux backup timers,
  including local dumps, local freshness, the legacy mirror and remote jobs.
- `[remote_backup]`: destination, SSH/key/password-file paths, disk guards,
  bandwidth and remote snapshot retention (`keep_daily`).

Database names come exclusively from the sibling `.env` (`DB_NAME` and
`PG_ORTHANC_DB`). The remote runner rejects the former `database_names` and
`max_age_hours` keys in `[remote_backup]` so old duplicated settings cannot
silently survive. Passwords/private keys stay in protected files; their paths
are configured in TOML. Neither unit templates nor the remote host have a
second copy of destination/source configuration.

`config.example.toml` documents the schema and provides the default schedule
entries when omitted from the per-host config. Unit templates contain schedule
placeholders only. Changing schedules requires re-rendering/reloading the
installed units; changing runtime paths/policy takes effect at the next run.
The historical macOS-source launchd calendars are unchanged; this schedule
renderer targets the current Linux source, including when its destination is
a Mac. Existing local shell-script environment overrides remain available for
explicit one-off use, as documented in the main backup strategy.

For a synthetic test, `--config PATH` selects an isolated TOML file and uses
`.env` from the same directory. Set **both** synthetic source roots and both
database names there; do not point a smoke test at live inputs.

## Coverage and schedule

| Repository | Input | Suggested source-host local schedule |
|---|---|---|
| `tier1` | Newest completed dump and SHA-256 sidecar for each DB, plus the newest Orthanc storage snapshot and sidecar | 03:15, with up to 5 minutes jitter |
| `imaging` | `config.toml [storage].cold_archive_root`, excluding `*.tmp` and `*.partial` | 03:30, with up to 10 minutes jitter |
| Freshness | Success records and actual remote snapshot existence for both repositories | Hourly |
| Maintenance | Repository check (random 1/12 of data), then retention and prune | Sunday 06:00 Tier 1 / 08:00 imaging |

**What is copied, and what is not.** Tier 1 uploads exactly six files: the
newest completed dump and its `.sha256` sidecar for each of the two databases,
plus the newest Orthanc storage snapshot and its sidecar. Imaging uploads the
canonical cold archive tree under `[storage].cold_archive_root` only, which
contains one `.tar.zst` per series. The warm cache under
`[storage].dicom_data_root` (series extracted for viewing, which changes daily
and is rebuilt from the archives) is a separate tree and is never read; nor is
Orthanc's on-disk index, which the `orthanc_db` dump covers. Confirm what a
running job reads with `ps -o args= -C restic`: the paths after `--` are the
only inputs.

Only the newest completed Tier 1 set is uploaded each night. Existing local
history is not automatically imported. Remote retention keeps 60 daily
recovery points by default, independently of local file rotation. Maintenance
groups snapshots by host and tier tag, **not source paths**, because dated
dump filenames change each night. Weekly pruning means some extra snapshots
remain between maintenance runs. Source deletions remain recoverable from
retained snapshots. Incomplete restic backups never advance the success record.

The two repositories run independently, so an initial imaging upload does
not lock out database uploads. Jobs retry up to three starts per two-hour
window, at 15-minute intervals. Overlapping mutations in the same tier fail
without touching the repository. Freshness may read the last committed success
while an upload is running. No production data is sent unless explicitly
configured and invoked; installing units does not enable the new timers.

## Configure the production destination

1. Confirm the destination IP, user, mounted backup disk, encryption, free
   capacity, and expected availability. The root must be an existing directory
   **below** the configured mount point, with no symlink redirects. Allow space
   for the initial archive tree, DB/Orthanc history, changed archives and growth.
   `.tar.zst` and compressed dumps should not be assumed to compress further.
   Check live sizes with `du -sh` on the two source roots and `df -h` remotely.
2. Install restic on the source from a verified release or trusted package.
   The tested release and installation instructions are available from the
   [restic project](https://restic.readthedocs.io/en/stable/020_installation.html).
3. Generate a dedicated Ed25519 key as the deployment user if one does not
   already exist. For unattended jobs the key must be usable without prompts.
   Verify the destination host fingerprint through a trusted login, then use
   `ssh-copy-id -i ~/.ssh/id_ed25519_pacs_backup.pub USER@HOST` once interactively.
   The private key stays on the source. The runner uses `BatchMode=yes`,
   `IdentitiesOnly=yes` and `StrictHostKeyChecking=yes`.
4. On the destination, restrict the dedicated key with `restrict` and, when
   stable, `from="SOURCE_IP"` in `authorized_keys`. These disable forwarding and
   interactive PTYs but still permit commands: the preflight needs Python and
   restic needs SFTP. Use a dedicated destination account and filesystem
   permissions if isolation from other destination files is needed. An
   unrestricted `corelab`/`pere` account is suitable for a controlled test but
   does not provide that isolation. Do not use a forced SFTP-only command with
   this runner, because it would block the mount check.
5. Generate an owner-only repository password file on the source; never put its
   contents in a command line, git, or the transcript. Example as the job user:

   ```bash
   install -d -m 0700 ~/.config/ssc-pacs
   python - <<'PY'
   import os, secrets
   from pathlib import Path
   path = Path.home() / '.config/ssc-pacs/restic-password'
   fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as stream:
       stream.write(secrets.token_urlsafe(48) + '\n')
   PY
   ```

   Escrow this password in the team's secret manager before using production
   backups. Losing the source and the only password copy prevents recovery.
6. Add `[remote_backup]` from `config.example.toml` to the deployment's
   `config.toml`, replace every placeholder, and set `enabled = true` only when
   ready for manual validation. This does not enable timers. Configure source
   mount points explicitly (`tier1_mount`, `imaging_mount`); `/` is allowed for
   a source genuinely on the root filesystem. Keep the password and state/cache
   directory outside the source trees. Existing state directories must be
   owned by the job user with mode 0700.

`min_free_gib` is a reserve. For Tier 1, each preflight also budgets the full
current dump/snapshot set. For an imaging destination without a local success
record, it budgets the entire source tree as well. Later imaging uploads check
the reserve; growth and retained changed versions still need capacity planning.
The password file and SSH private key must be mode 0600 (or stricter) and owned
by the job user. The remote root must not be shared with another source's
repositories. Keep `backup_id` stable.

The runner gets source paths from `[backup]` and `[storage]`; remote settings
live solely in `[remote_backup]`. Use the configured project Python (3.11+,
including the stack's `python-dotenv` dependency) for the following commands,
from the stack root.

## Manual validation, then activation

```bash
python scripts/backup/remote_backup.py init tier1
python scripts/backup/remote_backup.py init imaging
python scripts/backup/remote_backup.py backup tier1 --dry-run
python scripts/backup/remote_backup.py backup imaging --dry-run
```

`init` creates repositories and should be run once per repository. A backup
dry run sends no file contents and does not update success state, but restic
still reads and hashes every new file, so an imaging dry run over the full
cold archive takes as long as the read half of a real upload. Use it to prove
the preflight and repository access, then stop it; validate the full round trip
with a separate small synthetic source/config.
For production, the following commands perform real transfers:

```bash
python scripts/backup/remote_backup.py backup tier1
python scripts/backup/remote_backup.py backup imaging
python scripts/backup/remote_backup.py freshness tier1
python scripts/backup/remote_backup.py freshness imaging
python scripts/backup/remote_backup.py maintain tier1 --dry-run
python scripts/backup/remote_backup.py maintain imaging --dry-run
```

Backups print a JSON summary containing `snapshot_id`. Restore into a new,
absolute destination directory (an existing destination is rejected):

```bash
python scripts/backup/remote_backup.py restore tier1 \
  --snapshot SNAPSHOT_ID --target /path/to/new-restore-directory
python scripts/backup/remote_backup.py restore imaging \
  --snapshot SNAPSHOT_ID --target /path/to/new-imaging-restore-directory
```

Restic recreates source paths beneath the target and verifies restored data.
For a spot check or a single-series recovery, pass `--include SOURCE_PATH`
(repeatable, absolute source paths) so only those files are pulled instead of
the whole snapshot:

```bash
python scripts/backup/remote_backup.py restore imaging \
  --snapshot SNAPSHOT_ID --target /path/to/new-dir \
  --include /cold/root/<patient>/<study>/<series-dir>/<series>/DICOM.tar.zst
```
Tier 1 sidecars currently contain the producer's absolute paths: compare the
digest with the **restored** artifact, rather than running `sha256sum -c` against
a sidecar that could still reference the live source. Then rehearse actual DB
and Orthanc recovery using [`restore_runbook.md`](restore_runbook.md); a synthetic
file round trip alone does not validate PostgreSQL or Orthanc recovery.

After the production seed and restore checks, the operator runs:

```bash
sudo scripts/linux/install_systemd.sh
sudo systemctl enable --now pacs-remote-backup-tier1.timer pacs-remote-backup-imaging.timer
sudo systemctl enable --now pacs-remote-freshness.timer
sudo systemctl enable --now pacs-remote-maintain-tier1.timer pacs-remote-maintain-imaging.timer
```

The installer renders these templates but deliberately skips enabling them.
It also manages the existing stack services; use its `--dry-run` to review the
rendered units before installation. `Persistent=true` may start a missed run
immediately on timer activation. Do not enable the old cold mirror as well.
The stack start script resumes only remote timers already enabled by the
operator; `--enable` does not opt in new remote jobs. After a stack retirement
that disables them, explicitly enable the remote timers again.

## Day-2 operation

Once the units are installed, drive the jobs through systemd rather than a
shell: the units carry idle IO priority, journal logging, retries and survive
logout. Each command below is a one-off run; none of them enables a timer.

```bash
sudo systemctl start pacs-remote-backup@tier1.service     # a few minutes
sudo systemctl start pacs-remote-backup@imaging.service   # hours for the initial seed
sudo systemctl start pacs-remote-freshness.service
sudo systemctl start pacs-remote-maintain@tier1.service
```

`systemctl start` on these oneshot units blocks until the run finishes.
Ctrl+C (or closing the terminal) only stops the waiting; the job keeps
running. To actually abort a run use `sudo systemctl stop pacs-remote-backup@imaging`.
Aborting is safe: the runner never records success for an incomplete run, and
restic reuses the data already uploaded on the next run, so a stopped seed
resumes rather than restarts. Leftover partial packs are removed by the
weekly maintenance prune.

**Monitoring.** The runner captures restic's per-file progress and logs only
the preflight line (`free_bytes` on the destination) at start and the JSON
summary at the end, so the journal is quiet during a run. Use:

```bash
systemctl status pacs-remote-backup@imaging          # activating = running; inactive + SUCCESS = done
journalctl -u pacs-remote-backup@imaging --since today
# Bytes landed so far, to compare with `du -sh` of the source root
ssh -i ~/.ssh/id_ed25519_pacs_backup -o IdentitiesOnly=yes USER@HOST \
  'du -sh /path/to/remote_root/imaging'
python scripts/backup/remote_backup.py freshness imaging   # last success + snapshot still present
```

A failed run logs `Remote backup failed: <reason>` and systemd retries up to
three times at 15-minute intervals within two hours, then gives up until the
next timer firing. **Initial seed expectations:** the archives are already
compressed, so bytes on the destination track source bytes almost one to one
and a saturated gigabit link moves roughly 6 GB per minute; size the first
run from `du -sh` of the cold archive root and expect it to take hours. Nightly
runs afterwards send only new or changed archives.

## Monitoring and recovery limits

- Logs: `journalctl -u pacs-remote-backup@tier1 -u pacs-remote-backup@imaging`.
  Failures return nonzero. Connect `OnFailure=` to the site's chosen alert
  service; there is no notification destination configured automatically.
- Freshness checks both upload completion and source age (default 36 hours).
  Copying yesterday's dump again does not reset its source age. Imaging uses
  the start time of the last successful scan, not archive/directory mtimes.
- `check TIER` reads **all** repository data: use it for a small test or a
  planned full integrity drill. Weekly maintenance reads a random subset;
  this does not guarantee every pack is covered within twelve weeks.
- The disks are checked before operations. A mid-transfer unmount causes the
  transfer to fail; encrypted external disks must be unlocked and mounted
  after reboot. A sleeping test Mac may also interrupt transfers.
- A writable SFTP repository is not immutable. For protection from source-host
  compromise, add destination-controlled snapshots or an independently
  administered append-only backend. Versioning protects normal source deletion.
- Source trees remain live while captured. The existing database and Orthanc
  snapshots are independently timed, not an atomic stack snapshot. Ingestion
  publishes archives by rename; unfinished `.tmp`/`.partial` files are excluded.
- Deployment secrets/configuration, PostgreSQL roles/grants, and recovery of
  the patched Orthanc image still need their separately documented recovery
  process; these jobs cover the four data artifacts, not every host file.
- This destination is a second-machine copy; a separate failure domain/offsite
  copy is a further deployment choice. No RTO is promised until a realistic
  restore is timed. Rehearse database and representative imaging recovery
  quarterly.
