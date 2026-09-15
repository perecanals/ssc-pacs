# Data Exports

Data Exports is the optional research-table browser at `/data-exports`. It uses
Navigator's login, theme and full-width workspace.

Admins can query all datasets. Staff use their existing dataset grants and can
manage only their own reports, export history and files. Admins can manage all
reports and exports. Ordinary users cannot access this module. Every API request
checks the current role; research data is read-only.

## Build an export

1. **Choose a dataset.** The selector sits below **Build export / Export history**
   and above the export controls. Admins can select **All datasets**; staff can
   select **All permitted datasets**. This scope applies to value suggestions,
   previews and exports in both builder and SQL modes. Changing it clears the
   preview. History is filtered by the dataset recorded when an export was made.
2. **Enter an export name.** CSV and Excel exports require 1–120 characters and
   cannot use a whitespace-only name. The API trims surrounding whitespace and
   enforces the requirement. Names appear in history, details and filenames;
   duplicate names are allowed. Previews do not require a name.
3. **Choose tables and columns.** A new table starts with up to eight metadata
   columns selected; instrument and unassigned labels start unchecked. Search
   columns or filter by instrument, then **Select shown** or **Deselect shown**.
   Other selections are preserved. The right-hand list controls output order:
   drag anywhere on a row, use its arrows, or click **×** to remove it. Instrument
   names appear in brackets with consistent, non-black colors.
4. **Add conditions and sorting.** Use the controls described below. Add a unique
   final sort column for stable pagination; separate previews can see different
   database snapshots.
5. **Preview or export.** Previews show 200 rows per page. CSV/XLSX exports include
   all matching rows within the configured resource limits and continue running
   after you leave the page. History shows progress, cancellation, errors and
   download links. Completed files expire after the configured retention period.

### Relationships

The builder uses predefined left joins between labelled patients, studies and
series, plus series-to-DICOM-tags. Patient and series join directly on
`patient_id` in either direction; study is not required. When study and series
are both selected, their join uses `studyinstanceuid` to avoid matching unrelated
studies from the same patient.

Unmatched base rows remain in a left join, and multiple matches produce multiple
output rows. Choose the base table for the desired patient/study/series level.

### Conditions and existing values

Condition columns are listed alphabetically. Groups combine children with
**AND** or **OR**; **NOT** negates the group. The builder supports three nested
group levels. For `(A OR B) AND NOT (C AND D)`, create an OR group and a negated
AND group inside the root AND group. PostgreSQL NULL logic applies: negation
does not automatically include NULL values; add **is empty (NULL)** when needed.

Select-type labels automatically show existing values. For other columns,
**Choose existing value** opens a searchable list. Suggestions come from the
column within the active dataset and permitted scope; other conditions and
joins do not narrow the list. Search is literal and case-insensitive, with up to
100 matches shown. Values over 4,096 bytes require manual entry. NULL is omitted;
empty strings appear as **(empty string)**.

| Condition | Behavior |
|---|---|
| Equals / does not equal | Exact typed comparison |
| Is one of (IN) | Select or enter 1–1,000 exact values; selections survive searches |
| Contains, text | Literal case-insensitive substring |
| Contains, array | Match an individual array element |
| Other array comparisons | Use complete PostgreSQL array literals |
| Is empty / is not empty | Test SQL NULL, not an empty string |

Date fields accept `YYYY-MM-DD`. Timestamps accept `YYYY-MM-DD HH:mm:ss`, with
up to six fractional-second digits. Native calendar/time controls may display
local formatting, but query values use ISO format. Timestamp-with-timezone
conditions use UTC; timestamp-without-timezone conditions are not shifted.
Invalid dates are rejected before preview, save or export.

### Codebook

**Codebook** opens a dialog with annotation descriptions from
`label_definitions.description`. It starts on the current table and can be
filtered by table, instrument or search text. Each entry shows the label name,
SQL column, instrument, level, type and description. Missing descriptions are
identified explicitly. Definitions are shared across datasets. Close with
**Close** or Escape; browsing the codebook does not change the export.

## Saved reports and past exports

**Saved reports** (admins) or **My reports** (staff) holds reusable query setups,
not generated files. Saving a report uses the export name. Reports retain the
configuration, creator, last editor and timestamps. Deleting a report leaves
export history intact.

**Export history → Edit export** restores the name, dataset, columns, ordering,
conditions and SQL from a past run. Exporting the changes creates a new history
entry with `configuration.source_export_id` pointing to the original. The
original request and file remain unchanged until normal expiration. Reruns use
current data and revalidate current permissions. A saved report changes only
when you explicitly save it.

Each export records its name, configuration, SQL, bound parameters, format,
serialization version, requester, timestamps, status, row count, file size and
failure reason. **Details** shows equivalent SQL and download requests. A
download audit records accepted delivery, not proof that every byte reached or
was opened by the client. An unavailable audit store prevents submission or
delivery.

The worker rechecks role and dataset access before execution. Submitted queries
retain their authorized dataset scope: later grants do not widen an existing
export. Revoking required grants prevents execution or file download. Owners
can still inspect their historical configurations. Polling does not extend login
inactivity; after signing in again, users can find their jobs in history.

## Catalog and SQL

`web-app/data_exports/policy.py` exposes four tables:

- `patient_labelled`
- `image_study_labelled`
- `image_series_labelled`
- `series_dicom_tags`

Only existing, granted ordinary tables are listed. Staff and admins see the same
column catalog, including storage-path metadata in the labelled mirrors.
Navigator's copy-path controls remain admin-only. New tables are never exposed
automatically.

Labelled mirrors are eventually consistent and contain that level's labels.
They do not apply Navigator's annotation inheritance or clinical-date fallback.
New label columns appear through introspection. Internal reads of
`label_definitions` supply label metadata; `patient.dataset` supplies authoritative
cohort membership, so a stale mirror cannot preserve revoked cohort access.
These internal tables cannot be selected in the UI or submitted SQL.

Dataset restrictions are applied to every physical table reference before
aggregates and limits, including CTEs, subqueries and joins. Studies and series
link to patients by `patient_id`; tags link through their series. Scoped queries
exclude rows without a matching patient. User conditions can narrow this scope.

**SQL query** accepts one SELECT using approved tables, functions, types and
operators. Ordinary joins, non-recursive CTEs, subqueries, CASE, grouping,
ordering, windows and approved aggregates are supported. Writes, SELECT INTO,
locking, recursive queries, system catalogs and administrative commands are
rejected. Exact allowlists live in `data_exports/query.py`. Functions/types are
qualified to `pg_catalog`, tables to `public`, and inheritance scans are disabled.

After preview, switching to SQL copies the validated base query without baking
in the dataset selector's restriction. Returning to the visual builder restores
its previous settings; SQL edits are not reverse-engineered into builder controls.

## File formats

Each export reads one repeatable-read snapshot. Rerunning its SQL reads current
data; only the retained file guarantees identical downloaded bytes.

| Format | Representation |
|---|---|
| CSV | UTF-8 with BOM, standard quoting and a header. Potential spreadsheet formulas are prefixed with an apostrophe. Import identifier columns as text to preserve leading zeros. |
| XLSX | Strings and high-precision numbers remain text; ordinary numbers and booleans use typed cells. Formulas and automatic links are disabled. Rows split across sheets with repeated headers. |

Both formats use `\N` for NULL, ISO dates/times (session timezone UTC), and JSON
text for arrays/objects. A literal `\N` string looks the same as NULL; include
`column IS NULL` as an extra output column if that distinction matters. Limits
fail the job explicitly rather than silently truncating results. Download names
use an ASCII fallback and encoded UTF-8 filename for Unicode compatibility.

See [operations](../operations/data_exports.md) for setup, limits, recovery and
removal.
