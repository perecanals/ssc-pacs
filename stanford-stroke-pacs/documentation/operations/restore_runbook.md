# Restore runbook

Use this when a database is lost, corrupted, or accidentally truncated.
Backup strategy and RTO/RPO targets live in
[`backup_strategy.md`](backup_strategy.md).

**Audience:** any maintainer with `sudo` and `psql` access. The procedure
should be doable cold (no prior rehearsal in the same session) — if any
step is unclear, fix the doc, not your memory.

---

## What a complete recovery restores

A full recovery touches four independent backup artifacts (all under
`/DATA2/pg_backups/`, except the cold mirror). Restore whichever you lost, in
this order:

1. **`stanford-stroke`** — Web App data: annotations, users, labels, prefs — §1.
   *Fatal loss; no other copy.*
2. **`orthanc_db`** — Orthanc's index — §2.
3. **Orthanc storage volume** (`<project>_ssc-orthanc-storage`) — the **only
   copy** of OHIF-authored SR annotations + the Folder Indexer `indexer-plugin.db`
   — §2d. Restore it **together with `orthanc_db`**: they reference each other by
   attachment UUID, and the indexer DB's container-relative paths
   (`/dicom-data/...`) make the volume portable as long as the mount point stays
   `/dicom-data`.
4. **Cold DICOM archives** — §3 (dev: re-ingest from source instead; prod: from
   the Tier 2 mirror).

Pre-flight (§0) and the acceptance drill (§5) apply to all of them. For moving
everything to a **new host**, see [`cluster_migration.md`](cluster_migration.md).

---

## 0. Pre-flight (every restore)

```bash
# 1. Confirm latest backups exist and are recent
ls -lh /DATA2/pg_backups/stanford-stroke/ /DATA2/pg_backups/orthanc_db/ \
       /DATA2/pg_backups/orthanc_storage/

# 2. Verify checksums (catches silent disk corruption)
cd /DATA2/pg_backups/stanford-stroke && sha256sum -c latest.dump.sha256
cd /DATA2/pg_backups/orthanc_db     && sha256sum -c latest.dump.sha256
cd /DATA2/pg_backups/orthanc_storage && sha256sum -c latest.tar.gz.sha256   # if restoring the volume

# 3. Confirm the PG client major matches the server major
psql -h localhost -U perecanals -d postgres -c 'SHOW server_version;'
pg_dump --version    # must be >= server_version
```

If checksums fail or the client is older than the server, **stop** and
fix that first. A failed checksum means the dump is unusable — try the
previous timestamped dump in the same directory.

Always source `.env` for credentials rather than typing the password:

```bash
set -a; . /home/perecanals/pacs/stanford-stroke-pacs/.env; set +a
export PGPASSWORD="$DB_PASSWORD"
```

---

## 1. Restore `stanford-stroke` (Web App data)

This is the **fatal-loss** DB — annotations, users, label defs, preferences.

### 1a. Restore into a scratch DB first (always do this)

```bash
DEST=stanford_stroke_restore_test_$(date -u +%Y%m%d_%H%M)
createdb -h "$DB_HOST" -U "$DB_USER" "$DEST"

pg_restore \
    -h "$DB_HOST" -U "$DB_USER" \
    -d "$DEST" \
    --no-owner --no-privileges \
    --jobs=4 \
    /DATA2/pg_backups/stanford-stroke/latest.dump

# Spot-check
psql -h "$DB_HOST" -U "$DB_USER" -d "$DEST" -c "
  SELECT 'image_series'  AS t, count(*) FROM image_series
  UNION ALL SELECT 'annotations',     count(*) FROM annotations
  UNION ALL SELECT 'users',           count(*) FROM users
  UNION ALL SELECT 'label_definitions', count(*) FROM label_definitions;"
```

If counts look right, go to 1b. Otherwise try an older dump.

### 1b. Cut over to production

**Pause writers first** so you don't lose new annotations made between
"backup taken" and "restore performed":

```bash
sudo systemctl stop ssc-web-app
```

Then promote the scratch DB. Two options:

**Option A — rename swap (fast, atomic, no superuser DDL replay):**

```bash
psql -h "$DB_HOST" -U "$DB_USER" -d postgres -c "
  ALTER DATABASE \"stanford-stroke\" RENAME TO \"stanford-stroke-broken-$(date -u +%Y%m%dT%H%M)\";
  ALTER DATABASE \"$DEST\" RENAME TO \"stanford-stroke\";
"
```

**Option B — drop + restore in place:**

```bash
psql -h "$DB_HOST" -U "$DB_USER" -d postgres -c \
    'ALTER DATABASE "stanford-stroke" RENAME TO "stanford-stroke-broken-tmp";'
createdb -h "$DB_HOST" -U "$DB_USER" stanford-stroke
pg_restore -h "$DB_HOST" -U "$DB_USER" -d stanford-stroke \
    --no-owner --no-privileges --jobs=4 \
    /DATA2/pg_backups/stanford-stroke/latest.dump
```

Option A is preferred — the broken DB stays around (renamed) for forensic
analysis and you get a sub-second cutover.

Restart Web App and validate:

```bash
sudo systemctl start ssc-web-app
curl -sf http://localhost:8043/api/labels/summary | python3 -m json.tool | head
```

> **Alembic note:** the dump preserves the `alembic_version` table, so a
> restored DB is already at the same schema revision as the source. **Do
> not** run `alembic stamp` after a restore — Web App's `init_db()` will
> see head and emit no DDL. If you restored from a dump older than the
> current code's head revision, `init_db()` will roll the schema forward
> automatically. See [`schema_migrations.md`](schema_migrations.md).

Once you've confirmed the app is healthy for at least 24 h, drop the
broken DB:

```bash
psql -h "$DB_HOST" -U "$DB_USER" -d postgres \
    -c 'DROP DATABASE "stanford-stroke-broken-...";'
```

---

## 2. Restore `orthanc_db` (Orthanc index)

Orthanc's index is **rebuildable** from the on-disk DICOMs (the Folder
Indexer plugin re-scans on startup), but a restore from the dump is
faster than re-indexing 13k+ series. Prefer the dump.

### 2a. Restore into a scratch DB

```bash
DEST=orthanc_db_restore_test_$(date -u +%Y%m%d_%H%M)
createdb -h "$DB_HOST" -U "$DB_USER" "$DEST"

pg_restore \
    -h "$DB_HOST" -U "$DB_USER" \
    -d "$DEST" \
    --no-owner --no-privileges \
    --jobs=4 \
    /DATA2/pg_backups/orthanc_db/latest.dump

# Spot-check (Orthanc tables: resources, attachedfiles, metadata, etc.)
psql -h "$DB_HOST" -U "$DB_USER" -d "$DEST" -c "
  SELECT relname, n_live_tup
  FROM pg_stat_user_tables
  ORDER BY n_live_tup DESC LIMIT 10;"
```

### 2b. Cut over

```bash
# Stop Orthanc so it lets go of orthanc_db
docker compose -f /home/perecanals/pacs/stanford-stroke-pacs/docker-compose.yml down

psql -h "$DB_HOST" -U "$DB_USER" -d postgres -c "
  ALTER DATABASE orthanc_db RENAME TO orthanc_db_broken_$(date -u +%Y%m%dT%H%M);
  ALTER DATABASE \"$DEST\" RENAME TO orthanc_db;
"

docker compose -f /home/perecanals/pacs/stanford-stroke-pacs/docker-compose.yml up -d
docker logs -f ssc-orthanc | grep -i 'index\|ready\|http'
```

### 2c. (Last resort) rebuild from disk

If both `orthanc_db` and the most recent dump are unusable, drop the DB,
let Orthanc recreate the schema on first boot, and let the patched
Folder Indexer re-scan `/DATA2/pacs_imaging_data` (or the cold-warmed
copies). This takes hours and re-creates labels — read
`documentation/cold_storage/` and `scripts/orthanc/enrich_orthanc.py` first.

### 2d. Restore the Orthanc storage volume (OHIF SR annotations + indexer DB)

The `ssc-orthanc-storage` volume holds the **only copy** of OHIF-authored SR
annotations plus the Folder Indexer's `indexer-plugin.db`. Restore it **together
with `orthanc_db` (§2)** — they reference each other by attachment UUID. The
indexer DB's stored paths are container-relative (`/dicom-data/...`), so the
restored volume is portable across hosts as long as the new container keeps the
DICOM bind-mount at `/dicom-data` (no reindex needed).

```bash
ARCHIVE=/DATA2/pg_backups/orthanc_storage/latest.tar.gz
sha256sum -c "${ARCHIVE}.sha256"          # pre-flight integrity

COMPOSE=/home/perecanals/pacs/stanford-stroke-pacs/docker-compose.yml
VOL=stanford-stroke-pacs_ssc-orthanc-storage

# Stop Orthanc so nothing is using the volume
docker compose -f "$COMPOSE" down

# Wipe + reload the named volume. busybox tar reads the gzip stream; running as
# root in-container avoids host permission issues with the volume's files.
docker run --rm -i -v "$VOL:/vol" alpine sh -c 'rm -rf /vol/* && tar -xzf - -C /vol' < "$ARCHIVE"

docker compose -f "$COMPOSE" up -d
docker logs -f ssc-orthanc | grep -i 'index\|ready\|http'
```

Verify the SR annotations are back (expect the pre-incident count, e.g. 98):

```bash
set -a; . /home/perecanals/pacs/stanford-stroke-pacs/.env; set +a
curl -s -u "$ORTHANC_ADMIN_USER:$ORTHANC_ADMIN_PASSWORD" \
     -X POST http://localhost:8042/tools/find \
     -d '{"Level":"Series","Query":{"Modality":"SR"},"Expand":false}' \
  | python3 -c 'import sys,json; print("SR series:", len(json.load(sys.stdin)))'
```

To restore into a **scratch** volume instead (drill, no prod impact), swap `VOL`
for a throwaway name and skip the compose down/up.

---

## 3. Restore cold archives (DORMANT — production-only)

**Not applicable on dev** (no mirror is being maintained — DICOMs are
recoverable by re-running the image integration protocol against the
source data).

In production, once the cold mirror is enabled (see
`backup_strategy.md` §4):

```bash
# Stop hot warming first to avoid races
# (warming reads from cold_archive_root; if you're restoring INTO
# cold_archive_root from the mirror, no warm should be in flight)

# Restore everything
rsync -a --info=stats2 \
    "$COLD_MIRROR_DEST"/ /DATA2/pacs_imaging_data_compressed/

# Or restore a single series
rsync -a "$COLD_MIRROR_DEST/<series-uid-prefix>/" \
         /DATA2/pacs_imaging_data_compressed/<series-uid-prefix>/
```

Then verify by warming a recently-restored series:

```bash
curl -X POST -b cookies.txt http://localhost:8043/api/studies/<uid>/warm
```

---

## 4. Validate without touching production

For drills (or to vet a backup without disrupting users), restore into a
scratch DB and run read-only queries against it. Never alter the
production DB during a drill.

```bash
DEST=ws01_drill_$(date +%s)
createdb -h "$DB_HOST" -U "$DB_USER" "$DEST"
pg_restore -h "$DB_HOST" -U "$DB_USER" -d "$DEST" \
    --no-owner --no-privileges --jobs=4 \
    /DATA2/pg_backups/stanford-stroke/latest.dump

# Compare against production
for tbl in image_series annotations users label_definitions; do
    prod=$(psql -h "$DB_HOST" -U "$DB_USER" -d stanford-stroke -tAc "SELECT count(*) FROM $tbl")
    drill=$(psql -h "$DB_HOST" -U "$DB_USER" -d "$DEST"          -tAc "SELECT count(*) FROM $tbl")
    printf "%-20s prod=%s drill=%s\n" "$tbl" "$prod" "$drill"
done

# Tear down the drill DB
dropdb -h "$DB_HOST" -U "$DB_USER" "$DEST"
```

---

## 5. Acceptance gate — last successful drill

| Date | DB(s) | Result | Run by |
|---|---|---|---|
| 2026-04-15 | `stanford-stroke`, `orthanc_db` | PASS — row counts match production for `image_series`, `annotations`, `users`, `label_definitions`, plus top-10 Orthanc tables by row count | claude (WS01 acceptance) |

Run this drill quarterly. Update the table after each run.
