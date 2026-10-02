# Dataset layout on disk (v2.2)

How imaging is filed in the two storage roots, how the pre-v2.2 tree is moved
into that layout, how a study is relocated after a change of owner, and the
linked view. Identity model: [`linking_patients.md`](linking_patients.md).

## The layout

```
<dicom_data_root>/<slug>/<patient_id>/<StudyUID>/<series desc>/<SeriesUID>/DICOM/
<cold_archive_root>/<slug>/<patient_id>/<StudyUID>/<series desc>/<SeriesUID>/DICOM.tar.zst
```

- `slug` is the dataset slug of the enrollment that **owns** the study (the
  prefix of `image_study.patient_key`). It is not always the batch being
  ingested: a linked dataset re-sending shared imaging files it under its owner.
  Slugs are immutable, so renaming a dataset never moves files.
- `patient_id` is the imaging row's patient id (the DICOM PatientID).
- Two different people with the same id in two datasets get two folders
  (`crisp2-lvo/11-001`, `precise/11-001`). Before v2.2 they shared
  `<root>/11-001`.
- The archive tree mirrors the loose tree path for path. Orthanc indexes the
  loose tree as `/dicom-data/<slug>/<patient_id>/…`.

The **stored** paths are the source of truth for where a study is:
`image_study.study_path`, `image_series.dicom_dir_path` / `dicom_archive_path`
and `series_cache_state.cache_path`. Every reader follows them: warm and evict,
deletion, downloads, indexing, and new series appended to an existing study.
`web-app/storage_layout.py` defines where a study *should* be. A study whose
stored path differs is **misplaced**. It still works, but the audit reports it:

```bash
python scripts/data_integrity/dicom_path_sql_fs_audit.py   # "Misplaced studies: N"
```

A study becomes misplaced when its owner changes after ingestion.
`link_patients.py` and `split_merged_patients.py` re-own studies but never move
files: moving them needs Orthanc stopped. Both scripts print the misplaced count
after `--apply`.

## Moving studies: `scripts/migration/move_to_dataset_layout.py`

One idempotent tool covers three jobs:
- the one-off move of the pre-v2.2 tree;
- relocating misplaced studies later;
- rolling back.

It moves every misplaced study, and nothing once none are left.

For each study, three steps run in order. Each is written to a journal at
`<checkout>/maintenance/layout-move/<timestamp>/` before the next one starts:

1. **Files.** It renames the study dir in both roots. The roots are on the same
   filesystem, so these are renames, not copies. mtimes are kept, so Orthanc
   sees nothing new.
2. **Orthanc indexer.** It rewrites the `indexer-plugin.db` `Files.path`
   prefixes, `/dicom-data/<old>/` → `/dicom-data/<new>/`. This runs in a
   throwaway `python:3.12-slim` container (`scripts/migration/indexer_paths.py`).
   - It first backs the DB up inside the volume as
     `indexer-plugin.db.pre-layout-<ts>`.
   - Checks: row count unchanged, every attachment still has its file row,
     `integrity_check` ok.
   - Attachments resolve uuid → instanceId (a hash of the DICOM UIDs) → path,
     so this is all Orthanc needs; `orthanc_db` stores no paths.
3. **stanford-stroke.** It rewrites the stored path columns in one transaction:
   `image_study.study_path`, `image_series.dicom_dir_path`, `nifti_path` and
   `dicom_archive_path`, `series_cache_state.cache_path`, and the `*_labelled`
   mirrors. An audit runs before commit.

If any step fails, the script undoes the earlier ones: it rolls the DB back,
rewrites the indexer back, and reverses the renames. Afterwards it removes empty
old patient dirs and rebuilds the linked view. Files with no DB row are left in
place and stay in the old dir.

`--apply` refuses unless all of these are stopped:
- the Orthanc container;
- `ssc-web-app`;
- every backup, health and mirror timer, and any of their jobs still running;
- ingestion.

It checks with `systemctl`, `docker` and `pgrep`, and never runs sudo.

### Maintenance window (production)

From the stack root, in the `ssc-pacs` conda env. The steps you run with sudo
are marked `!`.

```bash
# 0. Dry run, any time: the plan, an indexer row count (read-only copy), preflight
python scripts/migration/move_to_dataset_layout.py

# 1. Stop everything (no ingestion running)
! sudo systemctl stop ssc-web-app pg-backup-stanford-stroke.timer pg-backup-orthanc.timer \
    pg-backup-freshness.timer orthanc-storage-backup.timer pacs-remote-backup-tier1.timer \
    pacs-remote-backup-imaging.timer cold-storage-health.timer
scripts/orthanc/dc.sh down

# 2. Back up both databases and the Orthanc storage volume (indexer DB + OHIF SRs)
./scripts/backup/backup_pg_db.sh stanford-stroke
./scripts/backup/backup_pg_db.sh orthanc_db
./scripts/backup/backup_orthanc_storage.sh

# 3. Move
python scripts/migration/move_to_dataset_layout.py            # preflight must be clean
python scripts/migration/move_to_dataset_layout.py --apply

# 4. Start everything
scripts/orthanc/dc.sh up -d
! sudo systemctl start ssc-web-app pg-backup-stanford-stroke.timer pg-backup-orthanc.timer \
    pg-backup-freshness.timer orthanc-storage-backup.timer pacs-remote-backup-tier1.timer \
    pacs-remote-backup-imaging.timer cold-storage-health.timer

# 5. Verify
python scripts/migration/move_to_dataset_layout.py             # "Nothing to move"
python scripts/data_integrity/dicom_path_sql_fs_audit.py       # 0 misplaced; sample paths exist
python scripts/data_integrity/disk_vs_db_series_audit.py --all
python scripts/data_integrity/reconcile.py
python scripts/cold_storage/prune_stale_index_paths.py         # dry run: nothing stale
```

Then open a few studies in OHIF, including a formerly shared id such as
`11-001` under both datasets, and warm and evict a cold series.

**Backups.** Restic deduplicates by content, so the move uploads no new
imaging data. But the path change defeats restic's file cache, so the next
imaging backup re-reads the whole archive tree once. Plan for its duration.

### Rollback

```bash
# Stack stopped as in step 1
python scripts/migration/move_to_dataset_layout.py --rollback maintenance/layout-move/<timestamp>
```

The rollback replays the journal backwards. It undoes only the steps the
journal shows were done (an interrupted run may not have reached the indexer or
the DB commit). The backups from step 2 are the last resort.

## Linked view

A person enrolled in two datasets (one subject) sees the studies another of
their enrollments owns, but only through the database. For browsing the files,
the linked view gives each enrollment symlinks to that shared imaging:

```
<linked_view_root>/<slug>/<patient_id>/<StudyUID>  ->  <cold_archive_root>/<owner's study dir>
```

- It is enabled by `config.toml [storage] linked_view_root`, an absolute path
  outside both storage roots (enforced at startup). It is unset by default.
- Targets are the archive tree, which is canonical in `cold_path_cache`.
- It is a **convenience view, not a source of truth**: generated, rebuildable
  at any time, and never walked by Orthanc, the audits, deletion or the backups.
- It is rebuilt after `link_patients.py`, `split_merged_patients.py` and
  `move_to_dataset_layout.py` apply. To rebuild it by hand:

```bash
python scripts/cold_storage/build_linked_view.py          # dry run
python scripts/cold_storage/build_linked_view.py --apply
```

The rebuild removes stale and dangling links and their empty dirs; it never
removes a file that is not a symlink.
