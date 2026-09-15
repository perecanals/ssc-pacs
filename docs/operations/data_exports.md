# Data Exports operations

See the [reference](../reference/data_exports.md) for workflows, authorization,
SQL restrictions and file formats.

## Setup

Install the pinned web-app requirements and build the frontend using the normal
deployment procedure. The module uses `pglast` and `XlsxWriter`. From the stack
root (`stanford-stroke-pacs/`), apply migrations before enabling it:

```bash
alembic upgrade head
python scripts/admin/manage_data_exports_db.py provision
python scripts/admin/manage_data_exports_db.py check
```

| Migration | Change |
|---|---|
| `0021_data_explorer` | Reports, export jobs and download audit tables |
| `0022_staff_role` | Staff flag on application users; admin/staff flags are mutually exclusive |
| `0023_export_names` | Required export name; existing records receive `Export <UUID>` |
| `0024_data_exports_naming` | Rename metadata tables, constraints, indexes and audit sequence; retain all rows |

These migrations do not change research rows or delete history. Restart the
backend after upgrades so routes match the schema.

Provisioning creates the dedicated `sscpacs-readonly` PostgreSQL login, or uses
`DATA_EXPORTS_DB_USER` if configured. It stores generated credentials in `.env`
(mode 0600) without displaying them. It refuses to modify a role without its
ownership marker. Running `provision` again rotates the password; restart the
app afterward to load it. For grant updates without rotation, use:

```bash
python scripts/admin/manage_data_exports_db.py sync
python scripts/admin/manage_data_exports_db.py check
```

The reader receives SELECT on the four export tables, plus `label_definitions`
and `patient` for internal metadata and dataset checks. It receives no future-table
or Orthanc grants. Startup rejects administrative privileges, role memberships,
write grants, access outside the catalog, schema CREATE privileges, and missing
internal metadata grants. PUBLIC grants are not globally changed; submitted SQL
cannot query system catalogs or access the application's write connection.

The role defaults to read-only transactions, a 30-minute statement timeout and
1 GiB PostgreSQL temporary-file limit. Query transactions use repeatable-read,
read-only mode, a constrained search path, 16 MiB `work_mem`, a two-second lock
timeout and UTC. SELECT-only grants still prevent writes if session settings
are changed.

Configure a private, absolute spool directory in the per-host `config.toml`:

```toml
[data-exports]
enabled = true
spool_dir = "/absolute/private/path/to/data-exports"
```

The directory must be owned by the app user, mode 0700 and empty on first use.
The app creates it if absent and writes a marker identifying managed temporary
storage. The example configuration leaves the module disabled.

After building and configuring, restart the service and refresh the browser:

```bash
sudo systemctl restart ssc-web-app
```

## Upgrade existing installations to the new naming

Existing installations must rename their host configuration and spool before
starting the updated backend. From the stack root, inspect the plan first:

```bash
python scripts/admin/migrate_data_exports.py
```

Then stop the service and apply the upgrade as the app user:

```bash
sudo systemctl stop ssc-web-app
python scripts/admin/migrate_data_exports.py --execute
cd web-app
npm run build
sudo systemctl start ssc-web-app
```

The script renames the module section to `[data-exports]`, the credential keys to
`DATA_EXPORTS_DB_USER` / `DATA_EXPORTS_DB_PASSWORD`, and the reader ownership
marker. It preserves passwords and grants. A spool folder named after the old
module is renamed to `data-exports` in the same parent directory; custom spool
names are retained. Existing artifacts move with it and keep their identifiers.
The private spool marker becomes `.ssc-data-exports-spool`.

The script defaults to a dry run, refuses conflicting settings or directories,
and uses the worker's advisory lock to require that it is stopped. It restores
filesystem changes on failure and can be rerun after a successful upgrade.
At startup, Alembic `0024_data_exports_naming` renames the metadata tables without
changing report configurations, job SQL, timestamps, names or download audits.
Completed files remain downloadable until their normal expiration; interrupted
jobs follow the existing restart-recovery behavior.

Update external API callers to `/api/data-exports` and refresh browser tabs after
the restart. There are no legacy route, configuration or credential aliases.
Shipped migration identifiers and historical audit SQL retain their original
names. Disabling the module does not replace this upgrade on an existing host.

## Staff accounts

Staff is an application role, separate from the PostgreSQL reader login. Assign
existing accounts without changing passwords, dataset grants or Orthanc access:

```bash
python scripts/admin/manage_users.py set-staff <user> [<user> ...]
python scripts/admin/manage_users.py list
python scripts/admin/manage_users.py set-staff <user> --remove
```

The command is atomic: a missing account or existing admin aborts the batch.
Role changes take effect on the next API request; refresh the browser to update
controls. `/api/me` exposes `is_staff`. Staff also have dataset-scoped
[ZIP/NIfTI downloads](../recipes/dicom_processing.md#web-app-downloads), which
remain available if Data Exports is disabled.

## Capacity and recovery

One in-process worker runs exports; an advisory lock prevents multiple app
processes from owning the queue. Use the repository's single-uvicorn-process
deployment. Two shared slots serve previews and value lookups.

Optional `[data-exports]` settings:

| Key | Default | Meaning |
|---|---:|---|
| `queue_limit` | 10 | Waiting exports, in addition to one running job |
| `timeout_seconds` | 1800 | Export execution deadline |
| `preview_timeout_seconds` | 15 | Preview/value-lookup deadline |
| `artifact_bytes` | 5368709120 | Per-job output and intermediate files |
| `spool_bytes` | 10737418240 | Total temporary storage |
| `reserve_bytes` | 21474836480 | Minimum free disk space |
| `retention_hours` | 24 | Completed-file download window |

Rows are fetched in batches; XLSX uses a constant-memory writer with disk-backed
XML intermediates. Budgets are checked between batches and after finalization,
so allow headroom for one batch and workbook compression. PostgreSQL may also
need temporary disk. Check host capacity with `free -h` and `df -h <spool_dir>`.
Preview pagination is capped at offset 100,000; use an export for larger results.

Jobs move from queued to running, then completed, failed or cancelled.
Cancellation interrupts active SQL and is checked between batches. Failures
remove partial files. On restart, queued/running jobs become failed with an
interruption reason; **Edit export** can rerun them. Expired files and orphaned
UUID job directories are cleaned automatically. Reports and audit history remain.
Completed files survive restarts until expiration. Back up PostgreSQL metadata;
exclude temporary exports from long-term backups.

Download files are opened before auditing delivery, avoiding a race with file
expiration. Response cleanup closes handles even if transfer fails or the client
disconnects.

## Troubleshooting

Use **Export history → Details** for status and configuration. Avoid logging row
contents or raw database exceptions; these may contain identifiers or filters.
If startup marks the module unavailable, check the service journal and run
`manage_data_exports_db.py check`. Synchronize grants before restarting when policy
changes. Missing internal grants can prevent dataset choices or label metadata.

A frontend rebuild does not reload Python routes. If an expected endpoint is
missing, check `/openapi.json`, restart the service and refresh the browser.
Never print `.env` credentials into diagnostics. Navigator remains available
when the Data Exports reader or worker fails to initialize.

## Extend or remove

Backend code lives in `web-app/data_exports/`; frontend code lives in
`src/modules/data-exports/`. `DataExports.jsx` coordinates state, `Conditions`
edits nested filters, and `ExportHistory` renders runs and details. Integration
consists of API registration, worker lifecycle, the frontend route, landing card
and background-poll session handling. API routes use `/api/data-exports`;
background polls use the `X-Data-Exports-Poll` header.

Adding tables requires an explicit policy change, review of column exposure,
relationship definitions, and grant synchronization. New roles must preserve
server-side row scope in `scope.py` and artifact ownership checks in `access.py`.

To disable, set `enabled = false` and restart. To remove the module, remove its
packages, registrations, polling exemptions, config and exclusive dependencies.
Keep shipped migrations and audit tables; deleting retained metadata requires
a separate migration and retention decision. Shared staff authentication,
download filename/response helpers and imaging downloads are independent.
Revoke/drop the dedicated reader and remove its `.env` keys when no longer used.
Clean the spool only after stopping the worker and confirming that retained
files are no longer needed.
