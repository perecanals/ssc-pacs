# Data Explorer

The **Data Explorer** landing card opens `/admin/data-explorer`, an optional,
admin-only research-table browser and export module. It uses the same login,
top bar, theme and interaction conventions as Navigator. Every API endpoint
checks current administrator status, including direct artifact downloads.
Non-admin users cannot use this module, even when they have Navigator dataset
grants. Future role support must add both table/column policies and dataset
row restrictions before making any endpoint available to non-admin users.

## Build an export

1. Choose a research table. The initial selection prefers `patient_labelled`.
   Search the catalog or columns by name. Check the columns you want and use
   the arrows to set their output order.
2. Optionally add related tables. Relationships connect patient → study →
   series for the source tables and, separately, the labelled mirrors. A
   source series may also join `series_dicom_tags`. These are left joins:
   unmatched base rows remain; multiple matches create additional output rows.
   Choose the base table with the desired patient/study/series granularity.
3. Add conditions, nested **all/any** groups and sort columns. Comparisons use
   PostgreSQL column types. Text **contains** matches literal text, ignoring
   case; array **contains** matches one array element. Empty text differs from
   SQL NULL: use **is empty (NULL)** when appropriate.
4. Preview up to 200 rows per page. Preview requests time out separately from
   exports. Add a unique final sort column for stable pagination; separate
   previews may see database changes between requests.
5. Export CSV or Excel. Jobs run independently of the page; **Export history**
   shows rows written, status and errors and offers cancellation and download.
   Downloads use normal browser streaming, not an in-memory JavaScript blob.

No research rows can be added, edited or deleted through the module. The
query connection is a separate read-only PostgreSQL login. Reports and job
history are module metadata written by fixed SQL on the application connection.

## Catalog and SQL mode

The explicit catalog is defined in `web-app/data_explorer/policy.py`:

- `patient`, `image_study`, `image_series`;
- `patient_labelled`, `image_study_labelled`, `image_series_labelled`;
- `annotations`, `label_definitions`, `label_value_options`, `series_dicom_tags`.

Only existing, granted ordinary tables are listed. Labelled mirrors are
**eventually consistent** and contain that table level's labels; selecting a
mirror does not automatically apply Navigator's inheritance or clinical-date
fallback calculations. New label columns appear through live introspection.
Authentication tables, operational/audit tables and optional `clinical_data`
are excluded. New tables do not become accessible automatically.

**SQL query** accepts one SELECT statement, including ordinary joins,
non-recursive CTEs, subqueries, CASE, GROUP BY, ordering, window expressions,
and approved aggregates/functions. It is a deliberately restricted PostgreSQL
subset, not a general SQL console. Writes, SELECT INTO, locking, recursive
queries, system catalogs, unapproved tables, user-defined functions/types,
and administrative commands are rejected. Built-in functions/types are
qualified to `pg_catalog`, research relations to `public`, and implicit
inheritance scans are disabled. The exact supported expression/function lists
live alongside validation in `web-app/data_explorer/query.py`.

For example:

```sql
SELECT p.patient_id, count(s.studyinstanceuid) AS study_count
FROM patient AS p
LEFT JOIN image_study AS s ON s.patient_id = p.patient_id
WHERE p.dataset @> ARRAY['example_dataset']
GROUP BY p.patient_id
ORDER BY p.patient_id
```

Preview exposes copyable SQL, including escaped filter values. After preview,
switching to SQL mode copies that query into the editor. Returning to the
visual builder restores its previous configuration; it does not reverse-engineer
SQL edits. SQL mode and the builder use the same catalog and database identity.

## Shared reports and audit

All admins share named reports. Save, update, copy, delete or load a report
using the controls above the builder. Creator, last editor and timestamps are
recorded. Deleting a report does not delete export history.

Each export records an immutable snapshot of its configuration, SQL and bound
parameters, format and serialization version, requesting user, timestamps,
status, rows written, size and failures. **Details** exposes equivalent SQL,
configuration and download requests (user/time). A download request means the
server accepted delivery, not proof the client received or opened every byte.
An unavailable audit store prevents submission/delivery.

The worker also rechecks the requester's admin status before executing a job.
All administrators may download completed exports during their retention window.
Polling does not extend login inactivity timers. A user whose session expires
can log in again and find their job in history.

Each export reads one repeatable-read snapshot. Saved SQL/configuration reruns
against **current** data, so later results may differ. Temporary artifacts
allow identical downloads until expiration; persistent historical dataset
snapshots are not stored. Schema changes can invalidate saved reports and are
reported as query errors rather than silently changing selected fields.

## File representation

CSV is UTF-8 with a BOM, standard quoting and a header row. Text is preserved
(including leading-zero IDs); spreadsheet applications can still infer types
when opening CSV, so import ID columns as text or choose Excel. Potential
spreadsheet formulas in strings are prefixed with an apostrophe.

Excel uses XLSX with text cells for string identifiers and high-precision
numbers, numeric cells for ordinary numbers, and boolean cells for booleans.
Formulas and automatic hyperlinks are disabled. Output splits across sheets
at Excel's row limit and repeats the headers. Excessive column counts, overly
long cells, storage limits and query timeouts fail the job explicitly; results
are never silently truncated.

In both formats, SQL NULL is represented as `\N`; dates/times use ISO-8601
(session timezone UTC), and arrays/JSON use JSON text. A literal text value
`\N` uses the same display as NULL; consumers needing that distinction should
include `column IS NULL` as a separate SQL result column. Each export's
configuration records these representation choices.

For enabling, credentials, limits, recovery and removal, see
[Data Explorer operations](../operations/data_explorer.md).
