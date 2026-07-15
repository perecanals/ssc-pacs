# Deleting studies and series

Removing a study or series cleanly means deleting it from the **three
independent places** it lives (none of which cascade):

1. **`stanford-stroke` DB** — `image_study` / `image_series` plus the soft-linked
   side tables (`series_cache_state`, `series_dicom_tags`, `annotations`) and the
   `*_labelled` mirrors.
2. **Orthanc** — the `orthanc_db` index + DICOMweb metadata caches (cleared by
   one REST `DELETE /studies|series/{id}`, **online**, no container stop),
   **plus** the Folder-Indexer's private `indexer-plugin.db` Files rows. The REST
   delete does *not* touch those Files rows — the patched plugin only reacts to
   Orthanc start/stop, not delete — so they are purged separately by a **Force**
   `POST /indexer/scan` of the now-empty loose subtree, *after* the files are
   removed (`RemoveFilesUnderPrefix` drops the rows; with the files gone the scan
   re-registers nothing). Ordering matters: a Force scan over files still on disk
   would re-register them and *resurrect* the study — so file removal always
   precedes the indexer purge.
3. **On disk** — the loose `dicom_dir_path` tree and the cold `dicom_archive_path`
   archives, under `<root>/<patient>/<studyUID>/…` in both `dicom_data_root` and
   `cold_archive_root`.

The shared logic lives in `web-app/deletion.py`, used by both the CLI and the
admin HTTP endpoints, which run the full sequence **Orthanc → DB → files →
indexer purge**.

> **Annotations are discarded, not migrated.** Deleting an entity removes its
> annotations; the removal is captured in `annotations_history` (append-only,
> attributed to the operator), so values stay auditable/recoverable — but nothing
> moves to another study/series. Decide up front whether any labels need copying
> elsewhere first.

## No sudo required

The web-app service user (`perecanals`) **owns both storage roots** — it already
deletes loose files there during cold-cache eviction — so file removal needs no
privilege escalation. Deletion is complete from either entry point (UI or CLI).
The safety gate for the irreversible file delete is the **path-safety guard**
(target must sit under a configured root and be ≥ `<patient>/<studyUID>` deep, so
it can never remove a root or a whole patient) plus **admin-only auth** on the
endpoint and a **typed `yes`** on the CLI — not OS permissions.

## Option A — CLI (`scripts/admin/delete_study.py`)

Dry-run by default; `--execute` needs a typed `yes` (no sudo). Paths are relative
to the stack root (`stanford-stroke-pacs/`).

```bash
# 1. Review — list a patient's null/empty-StudyDescription studies (a common
#    "faulty upload" signature). Read-only.
python scripts/admin/delete_study.py --patient <patient-id> --null-description

# 2. Dry-run the delete of specific studies (shows Orthanc id, series, files,
#    and how many annotations would be discarded).
python scripts/admin/delete_study.py --study <UID> [--study <UID> ...]

# 3. Execute — complete removal (Orthanc + DB + files + indexer purge), per target.
python scripts/admin/delete_study.py --study <UID> --execute

# Delete a single series (parent study row is preserved):
python scripts/admin/delete_study.py --series <UID> --execute

# Maintenance sweep: remove any on-disk study dir with no image_study row
python scripts/admin/delete_study.py --purge-orphan-files --execute
```

The delete is attributed in `annotations_history` to `$USER` (or `$SUDO_USER` if
you happen to run under sudo).

## Option B — Admin UI button

On the Studies/Series tables (and expanded sub-rows), admins see a **trash-icon**
action. It opens a confirmation modal showing the series count and the number of
annotations that will be discarded. Confirming performs the **complete** removal
— Orthanc, DB, on-disk files, and the indexer purge — the same as the CLI. The
response reports what was removed (`files_removed`, `indexer_purged`).

## Verifying a delete

```bash
# Orthanc no longer knows the study:
curl -s -u "$ORTHANC_ADMIN_USER:$ORTHANC_ADMIN_PASSWORD" \
  -X POST "$ORTHANC_URL/tools/lookup" -d '<studyUID>'   # → [] (empty)

# DB rows gone; history retained:
psql -d stanford-stroke -c "SELECT count(*) FROM image_study WHERE studyinstanceuid='<studyUID>';"        -- 0
psql -d stanford-stroke -c "SELECT count(*) FROM annotations_history WHERE entity_id='<studyUID>' AND operation='D';"  -- ≥1

# Indexer Files rows purged (0 after a full CLI delete or the sweep):
docker exec ssc-orthanc python3 -c "import sqlite3; \
print(sqlite3.connect('file:/var/lib/orthanc/db/indexer-plugin.db?immutable=1',uri=True) \
.execute('SELECT count(*) FROM Files WHERE path LIKE ?', ['/dicom-data/<patient>/<studyUID>/%']).fetchone()[0])"
```

Then `python scripts/data_integrity/reconcile.py` should report the affected
patient as clean.

## Notes & safety

- **Idempotent / re-runnable**: Orthanc delete (404 ⇒ ok), DB deletes (0 rows ⇒
  ok), and file removal (missing ⇒ ok) all tolerate partial prior runs. If a run
  fails midway, re-run it.
- **Path safety**: file removal refuses any target not under `dicom_data_root` /
  `cold_archive_root`, or shallower than `<patient>/<studyUID>` — it can never
  delete a storage root or a whole patient directory.
- **Storage mode**: the CLI requires `cold_path_cache` (the production layout).
