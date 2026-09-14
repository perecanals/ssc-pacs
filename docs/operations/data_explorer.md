# Data Explorer operations

User workflows, catalog, serialization and audit semantics:
[Data Explorer reference](../reference/data_explorer.md).

## Enable

From the checkout root, install the web-app requirements in the `ssc-pacs`
environment and build the frontend using the normal deployment procedure.
The module adds pinned `pglast` and `XlsxWriter` dependencies. Alembic revision
`0021_data_explorer` creates only module-owned reports, exports and download
audit tables; the app applies it on startup. It does not change research data.

From the stack root (`stanford-stroke-pacs/`):

```bash
python scripts/admin/manage_explorer_db.py provision
python scripts/admin/manage_explorer_db.py check
```

Provisioning creates `sscpacs-readonly` (or the existing `EXPLORER_DB_USER`
setting) with a generated password and stores `EXPLORER_DB_USER` and
`EXPLORER_DB_PASSWORD` in `.env`, restricting that file to mode 0600. It never
prints the password. It refuses to alter an existing role unless that role
has this provisioning script's ownership marker. Running `provision` again
rotates the credential and resynchronizes approved SELECT grants; restart the
app afterwards so its environment uses the new credential.

The role gets explicit SELECT grants only for existing approved research
tables in the configured research database. It does not receive Orthanc or
future-table grants. PostgreSQL PUBLIC privileges can still permit database
connections and catalog metadata reads; the module connects only to the
configured research database and rejects catalog SQL. It refuses privileged
roles, inherited role memberships, table grants outside its catalog and
schema CREATE privileges. It does not globally revoke PUBLIC privileges or
modify other users' access.

The role defaults to read-only sessions, a 30-minute statement timeout and a
1 GiB PostgreSQL temporary-file limit. Query transactions additionally set
repeatable-read, read-only, a constrained search path, 16 MiB `work_mem`, a
2-second lock timeout and UTC. SELECT grants enforce write denial even if a
read-only session setting is changed. Submitted SQL cannot access the
application's credential or metadata-writing connection.

Set the per-host `config.toml` section, using an absolute private directory
on a filesystem with sufficient free space:

```toml
[data-explorer]
enabled = true
spool_dir = "/absolute/private/path/to/data-explorer"
```

The app creates that directory with mode 0700; an existing directory must be
owned by the app user, mode 0700 and empty on first use. A marker identifies
its purpose. Use a dedicated directory: it is managed temporary storage.
The default in `config.example.toml` is disabled with no spool path.

After preparing configuration/build, the operator restarts the web app:

```bash
sudo systemctl restart ssc-web-app
```

Agents must ask the operator to run this command under the repository's
`sudo` rule. Open the Data Explorer card as an admin and preview a small query.
An invalid reader configuration disables this module with a 503 response;
Navigator remains available. Check the service log and run the role check
command before retrying. Never print `.env` values into diagnostic output.

## Capacity and lifecycle

There is **one in-process export worker**, independent of cold-storage warm
workers, plus two concurrent preview slots. PostgreSQL advisory locking
prevents two app processes from operating the same export queue. No Redis,
Celery or additional service is required. Deploy the enabled module using the
repository's single-uvicorn-process topology.

Optional keys in `[data-explorer]`:

| Key | Default | Meaning |
|---|---:|---|
| `queue_limit` | 10 | Waiting exports (one additional job may run) |
| `timeout_seconds` | 1800 | Export execution deadline |
| `preview_timeout_seconds` | 15 | Preview statement deadline |
| `artifact_bytes` | 5368709120 | Per-job output and intermediate-file budget |
| `spool_bytes` | 10737418240 | Total temporary storage budget |
| `reserve_bytes` | 21474836480 | Minimum free disk space |
| `retention_hours` | 24 | Completed-file download window |

Rows are fetched in batches and written incrementally; XLSX uses a
constant-memory writer with disk-backed XML intermediates. Disk budgets are
checked between batches and after finalization, so allow headroom for a batch
and workbook compression. Very wide rows or large JSON fields cost more than
narrow tables; query plans may also use PostgreSQL temporary disk. Preview
pagination is capped at offset 100,000; use exports for the full result.

Use `free -h` and `df -h <spool_dir>` to check current capacity rather than
relying on a historical host measurement. The tests exercise one million
synthetic CSV rows with a peak Python-allocation bound, plus spreadsheet
splitting, limits, cancellation and recovery.

Jobs transition queued → running → completed/failed/cancelled. Progress is
rows written, not a speculative percent from an expensive COUNT. Cancellation
interrupts active database execution and is checked between batches. Failures
remove partial artifacts. On restart, previously queued/running jobs become
failed with an interruption reason; the operator can reuse their configuration.
Expired files and orphaned UUID job directories are cleaned automatically.
History and report definitions are retained. Completed artifacts remain
usable across restarts until expiration. Backup normal PostgreSQL metadata;
exclude temporary export files from long-term backups.

Use **Export history → Details** to diagnose status and configuration. Do not
record row contents or raw database exceptions in service logs: queries may
contain identifiers or clinical filters. File download events are retained
separately from the immutable export request.

## Extend or remove

The module is isolated under backend `web-app/data_explorer/` and frontend
`src/modules/data-explorer/`. Integration consists of API registration,
lifecycle start/stop, one frontend route, the landing card, and background-poll
session handling. API routes live under `/api/data-explorer` and must be
registered before the SPA fallback. Tests cover denied direct access as well
as UI hiding.

Adding a table requires updating the explicit policy, reviewing its data and
column types, adding any intended visual relationships, and rerunning role
provisioning to synchronize grants. Do not use broad default SELECT grants.
Adding non-admin roles requires server-enforced dataset row scoping for
**both** generated and handwritten SQL, previews, reports, history and
artifacts; exposing a button or broadening `require_admin` is insufficient.

To disable, set `enabled = false` and restart. This removes the card and makes
module operations unavailable without deleting reports/history. To remove the
code, remove its package, frontend module, registrations, poll exemptions,
config section and dependencies. Keep shipped Alembic revisions and audit
tables for historical compatibility. Drop module tables only through a new,
explicitly approved migration after the desired audit retention period.
Revoke/drop the dedicated database role and remove its `.env` keys when it is
no longer used; it must not be shared with another service. Clean the dedicated
spool only after the worker is stopped and retained downloads are no longer
needed.
