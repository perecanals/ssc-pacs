# Archive volumes

The cold archive tree (`*.tar.zst`, one per series) can be spread over several
disks. Each disk holds one **archive volume**: a named root (`disk1`, `disk2`,
…) declared in `config.toml`. The loose/warm tree (`dicom_data_root`) stays a
single root on one disk.

Code: `web-app/archive_volumes.py` (the single source of truth), setup tool
`scripts/admin/manage_archive_volumes.py`.

## Model

- **Same layout on every volume.** Each root files imaging as
  `<root>/<dataset slug>/<patient_id>/<StudyUID>/<series desc>/<SeriesUID>/DICOM.tar.zst`,
  the same relative path as the loose tree ([dataset layout](../operations/dataset_layout.md)).
- **The stored path is the truth.** `image_series.dicom_archive_path` is
  absolute. The volume a series is on is the configured root that contains
  that path (`archive_volumes.volume_for_path`, symlinks resolved). Every
  safety check that asks "is this an archive path, and under which root?" uses
  that one function: deletion, ingestion cleanup, the orphan sweep.
  There is no volume column in the database and no schema migration.
- **One study, one volume.** All archives of a study live on the same volume.
  A study that already exists — re-sent, overwritten, reassigned to another
  PatientID, a retry of a failed compression — goes back to the volume it is
  on. Only *new* studies follow the batch's choice. A study whose archives span
  volumes or sit outside every volume is refused (HTTP 409 / an ingestion
  error), never re-filed on a guess.
- **Orthanc does not see volumes.** It indexes the loose tree only; warming
  extracts an archive from whichever volume holds it back into
  `dicom_data_root`. Adding or losing a volume never touches the Orthanc index.

## Configuration

```toml
[storage]
dicom_data_root = "/media/pacs-ssd-1/ssc-pacs/ssc-pacs-data/imaging_data"
default_archive_volume = "disk1"       # required once there is more than one volume

[storage.archive_volumes.disk1]
root = "/media/pacs-ssd-1/ssc-pacs/ssc-pacs-data/compressed"
required = true                         # boot dependency; outage is /healthz-critical

[storage.archive_volumes.disk2]
root = "/media/pacs-ssd-2/ssc-pacs/compressed"
# backup_repository = "imaging"         # restic repository name; default shared "imaging"

[storage.dataset_archive_volume]        # optional: default volume for a dataset's NEW studies
"GUIDE" = "disk2"                       # dataset name (as in the ingestion YAML) or slug
```

Validated at startup (`config.py` → `parse_archive_volumes`): roots absolute
and not overlapping each other, `dicom_data_root` or `linked_view_root`;
`default_archive_volume` and every `dataset_archive_volume` entry must name a
configured volume; unknown keys are errors (catches typos).

The pre-v2.8 single `[storage] cold_archive_root = "…"` still works: it is read
as one volume `disk1` with `required = true`, and logs a deprecation warning.
Having both forms is an error. `config.COLD_ARCHIVE_ROOT` survives only as "the
default volume's root" for callers that mean exactly that.

### Where new studies go

The ingestion run resolves one volume for the batch, in this order:

1. the YAML key `archive_volume: disk2`;
2. `[storage.dataset_archive_volume]` for the batch's dataset;
3. `[storage] default_archive_volume`.

The run log prints the volume and which rule chose it. Check before a run:

```bash
python scripts/admin/manage_archive_volumes.py check-dataset GUIDE
```

## Online / offline

A volume is **online** when its root holds the sentinel file `.ssc-volume`
(JSON naming the volume) written by `manage_archive_volumes.py init|adopt`.
An unmounted disk leaves a bare mount-point directory without the sentinel; the
wrong disk carries the wrong name. Both read as **offline** — never as "every
archive on that disk is missing".

| What | Offline volume behaviour |
|---|---|
| Service start | Always starts. One `WARNING startup: archive volume … is OFFLINE` line per volume. |
| `/healthz` | `archive_volumes.<name>.online = false` and a `warnings` entry. HTTP 503 / `degraded` only if the volume is `required`. |
| `/metrics` | `archive_volume_online{volume=…}` (0/1), `archive_volume_free_bytes{volume=…}`. |
| Warm (UI / API) | 503 `archive_volume_offline` naming the disk; the series row records `error: archive_volume_offline:<name>`. Series already warm keep working. |
| Downloads | Served from the warm copy if present, else 503 naming the disk. |
| Delete | Refused (503) for studies on that volume — the rows would go and the files stay behind. |
| Orphan sweep | Skips the volume (logged). |
| Ingestion | Pre-flight refuses a batch whose volume is offline; per study, an existing study pinned to an offline volume fails its series (logged, retriable). Nothing is written into the bare mount point. |
| Backups / mirror | That volume is skipped, the others still run, the job exits non-zero. |
| `cold_storage_health.py` | `WARNING` (CRITICAL if `required`). |

`required = true` additionally makes the volume a systemd boot dependency
(`RequiresMountsFor=` in the rendered units, via `install_systemd.sh`). Leave it
off for dataset disks so the stack boots without them.

## Adding a disk (new volume)

1. Mount the disk (yours, as root). Two options:
   - **Plain directory on an already-mounted disk** (e.g.
     `/media/pacs-ssd-2/ssc-pacs/compressed`): nothing to mount; `init` creates it.
   - **Dedicated mount point**: make the bare mount point immutable *before*
     mounting, so an unmounted disk can never be written into:
     ```bash
     sudo mkdir -p <root>
     sudo chattr +i <root>
     # /etc/fstab — nofail so a missing disk never blocks boot:
     #   UUID=…               <root>  ext4  defaults,nofail  0 2
     #   /path/on/other/disk  <root>  none  bind,nofail,x-systemd.requires-mounts-for=/path/on/other  0 0
     sudo mount <root> && sudo chown sscpacs:sscpacs <root>
     ```
2. Initialise it (dry-run first; refuses a non-empty root, a root on `/`, an
   overlap with any other root). `--execute` **requires root** — adding a disk
   is an admin act; run it with the project interpreter from the service account:
   ```bash
   python scripts/admin/manage_archive_volumes.py init disk2 <root>
   sudo "$(which python)" scripts/admin/manage_archive_volumes.py init disk2 <root> --execute
   ```
   It creates the root owned by the invoking user (`SUDO_USER`, or `--owner`),
   checks that user can write there, writes a root-owned sentinel (read-only to
   the app) and prints the `config.toml` snippet.
3. Add the `[storage.archive_volumes.disk2]` table (and, optionally, the
   dataset default) to `config.toml`; restart the web app
   (`sudo systemctl restart ssc-web-app`). Re-run `scripts/linux/install_systemd.sh`
   only if you set `required = true`.
4. Verify: `manage_archive_volumes.py status`, `/healthz`, and the backup dry run
   `python scripts/backup/remote_backup.py backup imaging --dry-run`.
5. Ingest with `archive_volume: disk2` (or rely on the dataset default).

### Upgrading from the single `cold_archive_root`

Write the sentinel into the existing tree, then switch the config:

```bash
sudo "$(which python)" scripts/admin/manage_archive_volumes.py adopt disk1 <current cold_archive_root> --execute
```

Replace `cold_archive_root = "…"` with `default_archive_volume = "disk1"` and a
`[storage.archive_volumes.disk1]` table (`root` = the old value,
`required = true`), then restart the web app. Until `adopt` runs, disk1 reads
as offline (and, being required, `/healthz` is degraded and the imaging
backup fails) — so adopt first.

## Backups

The remote imaging tier (`scripts/backup/remote_backup.py … imaging`) takes
**one restic snapshot per volume**, into the repository named by the volume's
`backup_repository` (default `imaging`, shared). Snapshots of `disk1` keep the
pre-volume tag set and state file (`imaging.json`), so its history and
retention continue; every other volume is tagged `volume:<name>` and has its
own state file (`imaging-<name>.json`). Retention (`maintain`) groups by
host+tags, so each volume ages out independently. A volume with no archives
yet is skipped (not an error). Each volume's source must be a real directory
on its mounted filesystem: `disk1` uses `[remote_backup].imaging_mount`,
others `[remote_backup].imaging_mounts.<name>` when set, else the mount point
found from the root. Restore names the volume: `remote_backup.py restore
imaging --volume disk2 --snapshot … --target …`. See
[remote backups](../operations/remote_backups.md).

The dormant rsync mirror (`mirror_cold_archive.sh`) mirrors each online volume
to `<cold_mirror_dest>/<volume>/` and never runs `--delete` against a volume
whose sentinel is missing.

## Tools that are volume-aware

| Tool | Behaviour |
|---|---|
| `manage_archive_volumes.py status` | per volume: online/reason, free space, archived series + GiB per dataset; warns about archive paths outside every volume |
| `archive_all_series.py` | each series to its study's volume; studies with none yet to `--volume` (default volume); refuses offline targets |
| `verify_and_repair_archives.py` | one volume per run (`--volume`, default the default volume); refuses an offline volume |
| `build_linked_view.py` | links point at the owner study's directory on its own volume |
| `reconcile_migration.py` | accepts every volume's prefix |
| `move_to_dataset_layout.py`, `repoint_host_paths.py` | **single volume only** — refuse to run while more than one is configured |

Moving existing studies between volumes (splitting a dataset across disks) is
not implemented yet: today a dataset is split by sending its *new* batches to
another volume (`archive_volume:` or the dataset default); existing studies
stay where they are.

## Host rebuild checklist

The volume → disk mapping lives partly outside the repo. On a new host:
restore `config.toml` (the volume tables), recreate the mounts (`/etc/fstab`,
`chattr +i` on dedicated mount points), and check each sentinel is present
(`manage_archive_volumes.py status`). If roots moved, `repoint_host_paths.py`
handles a single volume only (see above).
