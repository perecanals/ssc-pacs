# Companion frontend implementation detail

**Purpose:** Deep reference for React routes, `DataTable` behavior, and component responsibilities. For product-level Companion documentation see [`companion.md`](companion.md).

---

## Backend summary (for context)

The companion backend (`companion/app.py` + `companion/routes/`) is a FastAPI service that:

- creates its app-owned tables on startup (via Alembic migrations)
- runs schema migrations (`MIGRATE_SQL`) to evolve the schema
- serves the landing page at `/`
- serves the companion UI via SPA catch-all for all non-API routes
- exposes read APIs for browsing patients, studies, series, annotations,
  labels, and label definitions
- exposes write APIs for annotations and label definitions
- supports cross-level label filtering via a `label_level` query parameter
  on all listing endpoints
- attaches both own-level annotations and inherited parent-level annotations
  to every returned row
- resolves OHIF links by querying Orthanc's REST API
- rebuilds snapshot tables on demand via `POST /api/snapshots/refresh`

### Authentication

- user credentials are verified against `users`
- passwords are stored as bcrypt hashes
- successful login returns an HttpOnly JWT cookie named `auth_token`
- middleware refreshes the JWT on most requests to provide sliding expiration
- `/api/me` and `/assets/*` are intentionally excluded from token refresh

Authorization model:

- read endpoints are public
- write endpoints require a valid JWT
- `created_by` is always taken from the authenticated user, never from client
  input

`users.is_admin` exists in the schema and in `manage_users.py`, but the
current companion backend does not implement admin-only authorization rules.

---

## Routes and layout

The companion frontend is a React single-page application built with Vite and
Tailwind CSS. Source code lives in `companion/src/`; the production build is in
`companion/dist/`. Component styles are in co-located `.css` files.

The app has two routes:

- `/` — Landing page with card links to Companion, Orthanc Explorer 2, and OHIF
- `/app` — Companion annotation browser

The companion page (`Companion.jsx`) provides three hierarchical levels:
**Patients**, **Studies**, and **Series**. The level switcher lives in the
top bar. Switching levels resets all filters, clears the active preview
selection, and remounts the data table via a React `key` prop.

The companion page is decomposed into focused React components:

- `TopBar` — home link, level switcher, column selector portal target,
  Reset View button, label-definition trigger area, and login/logout
  controls
- `Sidebar` — search box, annotation label list grouped by level with counts,
  modality filter dropdown. Labels from all levels are always visible; clicking
  a label sets both the label name and its level as the active filter and
  temporarily forces that label column visible in the data table. Clearing the
  label filter removes that temporary column visibility unless the user had
  already enabled the column in saved preferences.
- `DataTable` — generic data table supporting all three levels. Configurable
  per level via `LEVEL_CONFIG` (endpoint, columns, sort, filters, expand
  behavior). Key features:
  - **Expandable rows**: Patient rows expand to show studies; study rows expand
    to show series. Expansion is triggered by clicking anywhere in the row.
  - **Nested expansion**: When viewing patients, expanded study sub-rows can
    further expand to reveal series (three-level nesting).
  - **Embedded preview coordination**: Study and series row clicks can drive an
    embedded OHIF preview pane. The most recently selected study/series row is
    visually highlighted.
  - **Sticky column headers**: The table header (column labels + filter
    inputs) stays pinned at the top of the scroll container during vertical
    scrolling. Uses `position: sticky` on the `thead`.
  - **Frozen first column**: A pin toggle button in the first column header
    freezes both the expand-arrow column and the first data column so they
    remain visible during horizontal scrolling.
  - **Drag-and-drop column reordering**: Column headers are draggable via
    the native HTML5 Drag and Drop API. Drop indicators show where the
    column will land.
  - **Scroll-limited subtables**: Series-level sub-tables (both direct
    study→series and patient→study→series) are capped at ~280px height with
    vertical scroll.
  - **Column selector**: Shows label columns from all levels grouped by level.
    The main table only renders columns at or above its own level (e.g. the
    patient table does not show series labels). Child/grandchild subtables
    render label columns at their own level.
  - **Cross-level label filtering**: When a sidebar label filter is active,
    the `label_level` parameter is sent to the backend so filtering works
    across the hierarchy (e.g. filtering patients by a series-level label).
  - **Server-persisted table state**: Column visibility, column order, sort,
    filters, and frozen-column state are persisted per user and per level in
    the `user_preferences` table. Preferences are loaded on mount and saved
    via a debounced PUT on changes (with immediate flush on unmount/tab
    close). A "Reset View" button in the top bar restores all table
    preferences to their defaults.
  - **Inline editing with stopPropagation**: Label cells in all table levels
    use `stopPropagation` to prevent expand/collapse when interacting with
    label controls.
  - **Live subtable refresh**: After any annotation mutation, `handleMutated`
    re-fetches the main table and all currently expanded child and grandchild
    rows so changes are immediately visible.
  - **Snapshot refresh**: Authenticated users can trigger a full snapshot
    rebuild from the table summary row.
- `InlineEdit` — handles `bool` (checkbox), `int` (number input), `text`
  (text input), and `select` (pill-style dropdown with search/create) label
  types. Accepts a `level` and generic `entity` prop so it can annotate at
  any level. Annotations are shared across all users (one value per
  entity+label); `created_by` is shown as a tooltip for audit traceability.
- `ColumnSelector` — dropdown for toggling columns. Labels are grouped by
  their level with headings (Patient labels, Study labels, Series labels).
  Column visibility and order are persisted server-side in
  `user_preferences`.
- `Pagination` — prev/next navigation
- `LabelDefModal` — form to create new label definitions with level and
  datatype selectors, including a select-type option builder with colored
  pills
- `PreviewPane` — lower OHIF iframe container with inline loading/error states
  and overlay controls for opening the current viewer in a new tab or collapsing
  the pane
- `AuthContext` — React context providing `currentUser`, `login()`, `logout()`,
  and automatic 401 interception

### Predefined columns per level

| Level   | Columns                                              |
|---------|------------------------------------------------------|
| Patient | Patient ID, Stroke Date                              |
| Study   | Patient ID, Acquisition Date, Modality, Study Description |
| Series  | Patient ID, Acquisition Date, Modality, Series Description |

Study- and series-level rows include an OHIF action button. The Actions
column is hidden entirely at the patient level since patients have no
direct OHIF action.

### Embedded OHIF behavior

- selecting a study row opens an embedded OHIF preview for the study while also
  keeping study expansion behavior
- selecting a series row opens an embedded OHIF preview using a
  series-filtered URL for that study
- the preview pane is hidden when there is no active selection or when the user
  collapses it
- the embedded preview currently uses an `iframe`, so switching to a different
  study or series causes a full OHIF reload inside the pane

Orthanc Explorer and OHIF links on the landing page are dynamically built from
`window.location.hostname + ":8042"`.

### Notes field

- the backend stores a `notes` field on annotations
- the current shipped frontend does **not** expose notes editing or display

---

## Snapshot tables (backend-driven)

The companion can rebuild three snapshot tables on demand via
`POST /api/snapshots/refresh`. Each snapshot is a `CREATE TABLE ... AS SELECT`
that joins the source table with pivoted annotation columns for that level's
label definitions:

- `snapshot_patients` — from `lvo_clinical_data` + patient-level annotations
- `snapshot_studies` — from `image_study` + study-level annotations
- `snapshot_seriess` — from `image_series` + series-level annotations

These tables are dropped and recreated on each refresh and are intended for
bulk export or periodic reporting.

**Table DDL, indexes, and cold-storage columns** are documented in [`data_stores.md`](data_stores.md); migrations are managed via Alembic (`companion/alembic/`).
