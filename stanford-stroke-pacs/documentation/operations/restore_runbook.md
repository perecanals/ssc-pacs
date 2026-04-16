# Restore runbook

Use this when a database is lost, corrupted, or accidentally truncated.
Backup strategy and RTO/RPO targets live in
[`backup_strategy.md`](backup_strategy.md).

**Audience:** any maintainer with `sudo` and `psql` access. The procedure
should be doable cold (no prior rehearsal in the same session) — if any
step is unclear, fix the doc, not your memory.

---

## 0. Pre-flight (every restore)

```bash
# 1. Confirm latest backups exist and are recent
ls -lh /DATA2/pg_backups/stanford-stroke/ /DATA2/pg_backups/orthanc_db/

# 2. Verify checksums (catches silent disk corruption)
cd /DATA2/pg_backups/stanford-stroke && sha256sum -c latest.dump.sha256
cd /DATA2/pg_backups/orthanc_db     && sha256sum -c latest.dump.sha256

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

## 1. Restore `stanford-stroke` (Companion data)

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
sudo systemctl stop ssc-companion
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

Restart Companion and validate:

```bash
sudo systemctl start ssc-companion
curl -sf http://localhost:8043/api/labels/summary | python3 -m json.tool | head
```

> **Alembic note:** the dump preserves the `alembic_version` table, so a
> restored DB is already at the same schema revision as the source. **Do
> not** run `alembic stamp` after a restore — Companion's `init_db()` will
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
