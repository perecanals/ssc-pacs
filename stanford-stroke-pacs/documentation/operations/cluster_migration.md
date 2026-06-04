# Cluster migration (porting the stack to a new host)

**Purpose:** move an existing deployment — both PostgreSQL databases, the
Orthanc index, and the DICOM archives — onto a new machine (e.g. Linux → macOS)
**without reindexing**. For the macOS-specific runtime setup that follows the
port, see [`../guides/deployment_on_mac.md`](../guides/deployment_on_mac.md).
For the general restore mechanics, see
[`restore_runbook.md`](restore_runbook.md); for the verification tooling, see
[`reconciliation.md`](reconciliation.md).

---

## The key principle

Orthanc only ever references the **container path** `/dicom-data/...`, never the
host path. The bind mount maps host `/DATA2/...` (or the new Mac path) →
container `/dicom-data`. So **host path changes are invisible to Orthanc** — as
long as the container mount point stays `/dicom-data` and the relative tree
underneath it is unchanged, the index stays valid on the new host.

Three things must travel, and they are keyed differently:

| Artifact | Lives in | Keyed on | Needs path fix? |
|---|---|---|---|
| Research/app DB (`stanford-stroke`) | PostgreSQL | — | host paths fixed via SQL backfill (§3) |
| Orthanc index (`orthanc_db`) | PostgreSQL | container path `/dicom-data` | no — keep mount point |
| Folder Indexer state (`indexer-plugin.db`) | `ssc-orthanc-storage` Docker volume | container path `/dicom-data` | no |
| Archive tree (`*.tar.zst`) | filesystem | relative layout | rsync preserving layout |

---

## 1. Port the SQL cluster

The deployment has **two databases in one PostgreSQL server**:
`stanford-stroke` (users, `patient`/`image_series`/`image_study`/`lvo_clinical_data`,
annotations, all web-app-owned tables) and `orthanc_db` (Orthanc's index).

`scripts/backup/backup_pg_db.sh` runs `pg_dump` on **one database at a time**.
Make sure your backup set includes **both** — a `stanford-stroke`-only dump is
not enough if you want to skip reindexing (§2).

```bash
# On the source host (custom format dumps):
./scripts/backup/backup_pg_db.sh stanford-stroke
./scripts/backup/backup_pg_db.sh orthanc_db

# On the target host, after creating empty DBs and the Orthanc role:
createdb stanford-stroke
./init_orthanc_db.sh                      # creates orthanc_db + role (reads ./.env automatically)
pg_restore --no-owner -d stanford-stroke  <latest>/stanford-stroke.dump
pg_restore --no-owner -d orthanc_db       <latest>/orthanc_db.dump
```

Notes:
- Restoring `stanford-stroke` brings Alembic's `alembic_version` along, so
  Web App will see the schema already at head and won't re-migrate on startup.
- `users` (bcrypt logins) travel in the dump. Re-run
  `scripts/admin/manage_users.py rotate-service-account` on the new host only if
  you want fresh Orthanc service-account credentials; otherwise the restored
  `.env` value still matches `orthanc_users.json`.

---

## 2. Port Orthanc (copy the DB, reset the path — no reindex)

In `cold_path_cache` mode the loose DICOM files are **not on disk** — they are
`.tar.zst` archives, and the index only survives their absence because of the
patched `RemoveMissingFiles: false`. The Folder Indexer can only index files
that are physically present, so a from-scratch scan on the new host would index
**nothing**. Reindexing would also throw away the index you just restored
(including OE2 labels and `enrich_orthanc` display values, which live in
`orthanc_db`).

So: **copy the index and reset the path — do not reindex.** "The index" is two
stores, both already covered above:

1. **`orthanc_db`** — restored in §1.
2. **`indexer-plugin.db`** — the Folder Indexer's SQLite state, which maps each
   attachment UUID ↔ `/dicom-data/...` path. It lives at
   `/var/lib/orthanc/db/indexer-plugin.db` **inside the `ssc-orthanc-storage`
   Docker named volume**. Without it Orthanc knows the studies exist but cannot
   read a single image. Migrate the volume explicitly:

   ```bash
   # Source host — export the volume:
   docker run --rm -v ssc-orthanc-storage:/v -v "$PWD":/out alpine \
     tar czf /out/orthanc-vol.tgz -C /v .

   # Target host — after `docker compose up` once created the empty volume:
   docker compose down
   docker run --rm -v ssc-orthanc-storage:/v -v "$PWD":/in alpine \
     sh -c "rm -rf /v/* && tar xzf /in/orthanc-vol.tgz -C /v"
   ```

3. **The archive tree** — rsync `cold_archive_root` to the new host, preserving
   layout. Use `-a` (it preserves mtime; the indexer keys on mtime/size, and
   though the warm/evict cycle self-heals mtime drift, keeping it intact avoids
   a churn of "Modified" re-registrations on the first scans):

   ```bash
   rsync -a --info=progress2 \
     user@source:/DATA2/pacs_imaging_data_compressed/  /Users/you/pacs/compressed/
   ```

**The only Orthanc config change** is the *host* side of the bind mount and the
storage paths — nothing inside the container changes:

- `docker-compose.yml`: the `volumes:` left-hand side → the new host path
  (right-hand side stays `/dicom-data`).
- `config.toml` `[storage]`: reset `legacy_dicom_root`, `cold_archive_root`,
  `hot_cache_dir` to the new host paths.
- `orthanc.json`: leave `RemoveMissingFiles: false` and `Folders: ["/dicom-data"]`
  unchanged.

At rest the hot cache is empty (exactly as on the source server now); warm-on-
demand extracts an archive → the container sees `/dicom-data/<tail>` → it
matches the restored index → OHIF loads, no re-ingestion.

> **Fallback if you cannot move the volume:** Orthanc will limp via rescan —
> each study works only after it is warmed *and* the next Folder Indexer scan
> (≤ `Interval` s) re-registers the materialized files. Migrating the volume
> avoids that window and is strongly preferred.

---

## 3. Repoint Web App's host paths

`image_series.dicom_dir_path` and `dicom_archive_path` are read by **Web App
natively on the host** (for warm/evict and NIfTI generation) — these do need
rewriting, because Web App does not go through the container. Backfill the
host prefix only:

```sql
UPDATE image_series
SET dicom_dir_path     = replace(dicom_dir_path,
                                 '/DATA2/pacs_imaging_data',
                                 '/Users/you/pacs/imaging_data'),
    dicom_archive_path = replace(dicom_archive_path,
                                 '/DATA2/pacs_imaging_data_compressed',
                                 '/Users/you/pacs/compressed');
```

**Constraint that ties this to §2:** swap only the *prefix*; keep the path
*tail* identical. The new prefix must equal the bind-mount host source, so that
`<new prefix>/<tail>` still maps to the same `/dicom-data/<tail>` the ported
index expects. Mismatched tails make warmed files unreachable by Orthanc even
though they exist on disk.

---

## 4. Verify the migration

Two scripts, in order. The first is migration-specific; the second is the
standard ongoing reconciliation.

### 4a. `scripts/migration/reconcile_migration.py`

Run on the target host immediately after §§1–3. It is **read-only** and checks
the four things that a port can get wrong:

1. **Storage config** — the `config.toml` roots actually exist on the new host.
2. **Orthanc index restored** — Orthanc is reachable and `/statistics` reports
   non-zero study/series counts (proves `orthanc_db` came over and the
   container is up).
3. **Indexer state volume** — `indexer-plugin.db` is present inside the
   `ssc-orthanc-storage` volume (the §2 step that is easy to forget). Skipped
   with `--skip-volume` if Docker is unavailable.
4. **Host paths re-pointed** — no `image_series` row still carries an
   un-migrated prefix, and every recorded `dicom_archive_path` exists on disk.

```bash
python scripts/migration/reconcile_migration.py            # full check
python scripts/migration/reconcile_migration.py --limit 500  # sample rows for a quick pass
```

Non-zero exit means at least one check failed; resolve and re-run before
trusting the deployment.

### 4b. `scripts/data_integrity/reconcile.py`

Once the migration check passes, run the standard two-DB reconciliation for the
full per-series diff (and to populate the Prometheus gauges). It compares
`SeriesInstanceUID` between `image_series` and Orthanc's index and confirms
referenced paths exist on disk, reporting four mismatch categories: *in DB not
in Orthanc*, *in Orthanc not in DB*, *dicom_dir_path missing*, and
*dicom_archive_path missing*. Full detail in
[`reconciliation.md`](reconciliation.md).

```bash
python scripts/data_integrity/reconcile.py            # human-readable summary
python scripts/data_integrity/reconcile.py --json     # JSON report under maintenance/reconciliation-reports/
```

A clean run — coverage ≈ 100%, zero mismatches — confirms the index, the
metadata, and the files on disk all agree after the port.

---

## 5. Order of operations (summary)

1. On source: `pg_dump` both DBs; export the `ssc-orthanc-storage` volume.
2. On target: build/pull the patched Orthanc image; create empty DBs; restore
   both dumps.
3. rsync the archive tree to the new host paths.
4. Edit `docker-compose.yml` (mount source) and `config.toml` (storage roots);
   `docker compose up -d`; import the volume; `docker compose up -d` again.
5. Backfill `image_series` host paths (§3).
6. `reconcile_migration.py`, then `reconcile.py`.
7. Smoke-test: click a study in Web App → it warms → OHIF renders.
