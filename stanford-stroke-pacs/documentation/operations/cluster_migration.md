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
| Folder Indexer state (`indexer-plugin.db`) **+ OHIF SR annotations** | `<project>_ssc-orthanc-storage` Docker volume | container path `/dicom-data` | no |
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
2. **`indexer-plugin.db` + the OHIF SR annotations** — the Folder Indexer's
   SQLite state (maps each attachment UUID ↔ `/dicom-data/...` path) **and** the
   **only copy** of OHIF-authored DICOM SR annotations both live inside the
   Orthanc storage volume at `/var/lib/orthanc/db`. Without it Orthanc knows the
   studies exist but cannot read a single image, and the annotations are gone.

   **Use the real volume name.** Compose prefixes the `docker-compose.yml`
   `volumes:` key with the project (the compose directory name), so the volume is
   `<project>_ssc-orthanc-storage` — e.g. `stanford-stroke-pacs_ssc-orthanc-storage`,
   **not** the bare `ssc-orthanc-storage`. Confirm before touching it:

   ```bash
   docker inspect ssc-orthanc \
     --format '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{"\n"}}{{end}}' \
     | grep /var/lib/orthanc/db        # prints the actual volume name
   ```

   Migrate it explicitly — or simply reuse the **nightly backup artifact** if one
   exists (`<backup_root>/orthanc_storage/latest.tar.gz`, where `<backup_root>`
   is `config.toml` `[backup].backup_root` on the source host; see
   [`backup_strategy.md`](backup_strategy.md) and `restore_runbook.md` §2d), which
   is already a consistent gzip tar of this volume:

   ```bash
   SRC_VOL=stanford-stroke-pacs_ssc-orthanc-storage   # ← from the inspect above
   DST_VOL=stanford-stroke-pacs_ssc-orthanc-storage   # target project name may differ

   # Source host — export the live volume (read-only), or copy latest.tar.gz:
   docker run --rm -v "$SRC_VOL":/v:ro -v "$PWD":/out alpine \
     tar czf /out/orthanc-vol.tgz -C /v .

   # Target host — after `scripts/orthanc/dc.sh up -d` once created the empty volume:
   scripts/orthanc/dc.sh down
   docker run --rm -v "$DST_VOL":/v -v "$PWD":/in alpine \
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

**The only Orthanc config change** is the storage paths in `config.toml` —
nothing in `docker-compose.yml`, `orthanc.json`, or inside the container changes:

- `config.toml` `[storage]`: reset `mode`, `dicom_data_root`, and
  `cold_archive_root` to the new host paths. The Orthanc
  `/dicom-data` bind-mount **source** is derived from these by
  `scripts/orthanc/dc.sh` (exported as `DICOM_MOUNT_SOURCE`) — you no longer edit
  the compose `volumes:` by hand.
- `orthanc.json`: leave the shipped `Indexer` block unchanged —
  `RemoveMissingFiles: false`, `Folders: []` (no continuous scan), and
  `ScanRoots: ["/dicom-data"]` (the allow-list for scoped `POST /indexer/scan`
  registrations).

At rest the hot cache is empty (exactly as on the source server now); warm-on-
demand extracts an archive → the container sees `/dicom-data/<tail>` → it
matches the restored index → OHIF loads, no re-ingestion.

> **Fallback if you cannot move the volume:** there is no continuous rescan
> (`Folders: []`), so Orthanc will not heal on its own — each study works only
> after it is warmed *and* you re-register its materialized folders via a
> scoped `POST /indexer/scan` (`scripts/cold_storage/scoped_index.py` /
> `reindex_missing_series.py`). Migrating the volume avoids all of that and is
> strongly preferred.

---

## 3. Repoint Web App's host paths

Host paths are read by **Web App natively on the host** (warm/evict, NIfTI gen,
labelled exports) — they must be rewritten, because Web App does not go through
the container. **It is not just `image_series`** — every host-path column across
the schema must be backfilled, or the leftovers stay silently broken until
something reads them (e.g. warming records each series' `cache_path` from its
`dicom_dir_path`). The full set (verify with the all-columns scan below):

| Table | Columns (loose tree → `imaging_data`) | Columns (archive → `compressed`) |
|---|---|---|
| `image_series` | `dicom_dir_path`, `nifti_path` | `dicom_archive_path` |
| `image_study` | `study_path` | — |
| `series_cache_state` | `cache_path` (holds the series' `dicom_dir_path`) | — |
| `image_series_labelled` (snapshot) | `dicom_dir_path`, `nifti_path` | `dicom_archive_path` |
| `image_study_labelled` (snapshot) | `study_path` | — |

```sql
-- Loose-tree prefix. The '…/pacs_imaging_data/%' guard (trailing slash) excludes
-- '…_compressed', so replace() can never corrupt an archive path.
UPDATE image_series          SET dicom_dir_path = replace(dicom_dir_path,'/DATA2/pacs_imaging_data','/Users/you/pacs/imaging_data') WHERE dicom_dir_path LIKE '/DATA2/pacs_imaging_data/%';
UPDATE image_series          SET nifti_path     = replace(nifti_path,    '/DATA2/pacs_imaging_data','/Users/you/pacs/imaging_data') WHERE nifti_path     LIKE '/DATA2/pacs_imaging_data/%';
UPDATE image_study           SET study_path     = replace(study_path,    '/DATA2/pacs_imaging_data','/Users/you/pacs/imaging_data') WHERE study_path     LIKE '/DATA2/pacs_imaging_data/%';
UPDATE series_cache_state    SET cache_path     = replace(cache_path,    '/DATA2/pacs_imaging_data','/Users/you/pacs/imaging_data') WHERE cache_path     LIKE '/DATA2/pacs_imaging_data/%';
UPDATE image_series_labelled SET dicom_dir_path = replace(dicom_dir_path,'/DATA2/pacs_imaging_data','/Users/you/pacs/imaging_data') WHERE dicom_dir_path LIKE '/DATA2/pacs_imaging_data/%';
UPDATE image_series_labelled SET nifti_path     = replace(nifti_path,    '/DATA2/pacs_imaging_data','/Users/you/pacs/imaging_data') WHERE nifti_path     LIKE '/DATA2/pacs_imaging_data/%';
UPDATE image_study_labelled  SET study_path     = replace(study_path,    '/DATA2/pacs_imaging_data','/Users/you/pacs/imaging_data') WHERE study_path     LIKE '/DATA2/pacs_imaging_data/%';
-- Archive prefix.
UPDATE image_series          SET dicom_archive_path = replace(dicom_archive_path,'/DATA2/pacs_imaging_data_compressed','/Users/you/pacs/compressed') WHERE dicom_archive_path LIKE '/DATA2/pacs_imaging_data_compressed%';
UPDATE image_series_labelled SET dicom_archive_path = replace(dicom_archive_path,'/DATA2/pacs_imaging_data_compressed','/Users/you/pacs/compressed') WHERE dicom_archive_path LIKE '/DATA2/pacs_imaging_data_compressed%';
```

Then prove nothing was missed — scan **every** text column for the old prefix:

```sql
DO $$ DECLARE r record; n bigint; BEGIN
  FOR r IN SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema='public' AND data_type IN ('text','character varying') LOOP
    EXECUTE format('SELECT count(*) FROM %I WHERE %I LIKE ''/DATA2/%%''', r.table_name, r.column_name) INTO n;
    IF n>0 THEN RAISE NOTICE '% . % -> % rows', r.table_name, r.column_name, n; END IF;
  END LOOP; END $$;
```

(`reconcile_migration.py` check [4/4] also verifies these columns are under a
configured root — empty strings, e.g. `nifti_path` for series with no NIfTI, count
as "no path", not as un-migrated.)

**Reset `series_cache_state`.** It is **host-specific runtime state** carried in
the dump: rows marked `status='hot'` describe files warm on the *old* host, absent
here, so the UI would skip warming and fail to render. Reset to cold (self-heals —
a genuinely-warm series re-detects its files on next access):

```sql
UPDATE series_cache_state SET status='cold', warming_started_at=NULL, error_message=NULL WHERE status<>'cold';
```

**Constraint that ties this to §2:** swap only the *prefix*; keep the path
*tail* identical. The new prefix must equal the bind-mount host source, so that
`<new prefix>/<tail>` still maps to the same `/dicom-data/<tail>` the ported
index expects. Mismatched tails make warmed files unreachable by Orthanc even
though they exist on disk.

> For the **macOS** runtime that follows this port (Colima instead of Docker
> Desktop, headless LaunchDaemons, Full Disk Access for external volumes), see
> [`../guides/deployment_on_mac.md`](../guides/deployment_on_mac.md).

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
3. **Indexer state volume** — `indexer-plugin.db` (and the OHIF SR annotations)
   are present inside the `<project>_ssc-orthanc-storage` volume (the §2 step
   that is easy to forget). Skipped with `--skip-volume` if Docker is unavailable.
4. **Host paths re-pointed** — no row in any host-path column carries an
   un-migrated prefix (`image_series` `dicom_dir_path`/`nifti_path`,
   `image_study.study_path`, `series_cache_state.cache_path`, and the
   `*_labelled` snapshots), and every recorded `dicom_archive_path` exists on disk.

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
archive paths exist on disk, reporting four mismatch categories: *in DB not
in Orthanc*, *in Orthanc not in DB*, *dicom_archive_path missing*, and
*orphaned annotations* (annotation rows whose entity is gone from the spine
tables). Full detail in [`reconciliation.md`](reconciliation.md).

```bash
python scripts/data_integrity/reconcile.py            # human-readable summary
python scripts/data_integrity/reconcile.py --json     # JSON report under maintenance/reconciliation-reports/
```

A clean run — coverage ≈ 100%, zero mismatches — confirms the index, the
metadata, and the files on disk all agree after the port.

---

## 5. Order of operations (summary)

1. On source: `pg_dump` both DBs; export the `<project>_ssc-orthanc-storage`
   volume (or copy its nightly backup `latest.tar.gz`).
2. On target: build/pull the patched Orthanc image; create empty DBs; restore
   both dumps.
3. rsync the archive tree to the new host paths.
4. Set `config.toml` (storage mode + roots — the DICOM mount derives from it);
   `scripts/orthanc/dc.sh up -d`; import the volume; `scripts/orthanc/dc.sh up -d` again.
5. Backfill `image_series` host paths (§3).
6. Stand up the service layer exactly as a fresh install — build the web app
   frontend and install the service units — per
   [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md)
   §5 Steps 6–8 (`pip install -r web-app/requirements.txt`, `npm ci && npm run
   build`, `sudo scripts/linux/install_systemd.sh` / `scripts/macos/install_launchd.sh`).
7. `reconcile_migration.py`, then `reconcile.py`.
8. Smoke-test: click a study in Web App → it warms → OHIF renders.
