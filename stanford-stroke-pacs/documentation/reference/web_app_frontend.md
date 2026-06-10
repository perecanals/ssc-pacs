# Web App frontend implementation detail

**Purpose:** Deep reference for React routes, `DataTable` behavior, and component responsibilities. For product-level Web App documentation see [`web_app.md`](web_app.md).

---

## Backend summary (for context)

The web app backend (`web-app/app.py` + `web-app/routes/`) is a FastAPI service that:

- creates its app-owned tables on startup (via Alembic migrations)
- runs schema migrations (`MIGRATE_SQL`) to evolve the schema
- serves the landing page at `/`
- serves the Navigator UI via SPA catch-all for all non-API routes
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
- `AuthContext` runs an **idle watchdog** that mirrors the backend sliding
  session: `api/client.js` records the last session-sliding request (skipping
  the same paths the backend `sliding_jwt` middleware skips), and `/api/me`
  carries `session_timeout_seconds`. Once idle that long — checked on an
  interval and immediately on tab `visibilitychange`/`focus` (covers
  laptop-sleep) — it drops the server cookie and dispatches `auth:expired`,
  so the SPA redirects to `/login?expired=1` proactively instead of waiting
  for the next request to 401.
- Web App also reverse-proxies `/ohif/*` and `/dicom-web/*` to Orthanc (see
  `web-app/routes/proxy.py`). Both require a valid JWT. End users never present
  credentials to Orthanc — Web App attaches the service-account credential
  from `.env` on every upstream call.

Authorization model:

- read endpoints are public
- write endpoints require a valid JWT
- DICOM zip download (`GET /api/series/{uid}/dicom-zip`) is **admin-only**
  (`Depends(require_admin)`) — bulk export is a privilege, not a public
  read; the DataTable download button is hidden for non-admins
- proxied OHIF/DICOMweb routes require a valid JWT (any logged-in user)
- `created_by` is always taken from the authenticated user, never from client
  input

`users.is_admin` is consulted by the `require_admin` dependency
(`web-app/auth.py`) used by `/api/admin/*` endpoints and the DICOM zip
download. It also gates the "Orthanc Explorer" Landing card and the
DataTable DICOM download button on the frontend — non-admins do not see them.
`/api/me` returns `{"username": ..., "is_admin": bool}` so the React
`AuthContext` can apply admin-only UI affordances.

---

## Routes and layout

The web app frontend is a React single-page application built with Vite and
Tailwind CSS. Source code lives in `web-app/src/`; the production build is in
`web-app/dist/`. Component styles are in co-located `.css` files.

The app has two routes:

- `/` — Landing page with card links to Web App, Orthanc Explorer 2, and OHIF
- `/app` — Web App annotation browser

The Navigator page (`Navigator.jsx`) provides three hierarchical levels:
**Patients**, **Studies**, and **Series**. The level switcher lives in the
top bar. Switching levels resets all filters, clears the active preview
selection, and remounts the data table via a React `key` prop.

The Navigator page is decomposed into focused React components:

- `TopBar` — home link, level switcher, column selector portal target,
  Reset Filters and Reset View buttons, label-definition trigger area,
  and login/logout controls
- `Sidebar` — search box, annotation label list grouped by level with counts,
  modality filter dropdown. Within each level the labels are grouped by
  instrument (instruments alphabetical, unassigned last) and ordered by label
  creation time (oldest first) within each instrument — the same
  `compareLabelDefsDefault` ordering (`utils/table.js`) used for the default
  data-table column order, so the quick-filter list and the columns stay
  consistent. Labels from all levels are always visible; clicking
  a label sets both the label name and its level as the active filter and
  enables that label's column in the data table — exactly as if it had been
  checked in the column selector (a one-time, persisted enable, so the
  ColumnSelector checkbox stays in sync). The column can then be hidden again
  from the column selector independently of the filter, and is not
  auto-removed when the label filter is cleared. `visibleKeys` is the single
  source of truth for column visibility (no separate forced-visible overlay).
- `DataTable` (`components/DataTable/`) — generic data table supporting all three
  levels. Split into focused modules: `index.jsx` (orchestrator), `ChildRows.jsx`
  (child/grandchild rendering), `TableHeader.jsx` (column headers + filter row),
  `SelectFilterControl.jsx` (dropdown filter), `useTableData.js` (data fetch),
  `usePreferencePersistence.js` (debounced pref save), `useDragColumns.js`
  (drag-reorder), `actions.js` (DICOM download, OHIF link, refresh). Shared
  utilities in `utils/colors.js` and `utils/table.js` (LEVEL_CONFIG, formatters).
  Configurable per level via `LEVEL_CONFIG` (endpoint, columns, sort, filters,
  expand behavior). All components use `prop-types` for runtime prop validation.
  Key features:
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
  - **Label-column header tooltip**: Hovering a label column header shows
    an instant, themed tooltip (`.dt__col-tip`, a CSS `:hover` toggle —
    not the slow native `title=`) with the label description plus
    instrument and data-type chips. Level is omitted (already conveyed by
    the `dt__level-hint` badge). Builtin columns have no tooltip.
  - **Frozen first column**: A pin toggle button in the first column header
    freezes both the expand-arrow column and the first data column so they
    remain visible during horizontal scrolling.
  - **Drag-and-drop column reordering**: Column headers are draggable via
    the native HTML5 Drag and Drop API. Drop indicators show where the
    column will land.
  - **Scroll-limited subtables**: Series-level sub-tables (both direct
    study→series and patient→study→series) are capped at ~280px height with
    vertical scroll. Sub-tables stay `width: 100%` (so the row background
    spans the full container) but carry a trailing empty spacer column with
    `width: 100%` that absorbs all leftover horizontal space — the real
    columns collapse to their content width and stay flushed left while
    rows still extend to the right edge. Wider sub-tables scroll
    horizontally (the spacer collapses to zero), mirroring the main table's
    behavior. `gcColSpan` includes the child table's spacer column so the
    grandchild wrapper spans the full child-table width.
  - **Auto-load infinite scroll**: The main table has no pager. Rows
    accumulate as you scroll: an `IntersectionObserver` sentinel row at the
    bottom of the list (rooted on the bounded `.dt__scroll` container, with a
    ~200px `rootMargin` prefetch) fetches the next offset page and appends it.
    The backend `ORDER BY` carries a fixed `ASC` unique-id tiebreaker
    (`patient_id` / `studyinstanceuid` / `seriesinstanceuid`) so appended
    pages never duplicate or skip rows on tied sort values. The DOM is
    unbounded (no cap / no virtualization). Any filter/sort/level change
    resets the accumulated list and scrolls back to the top; `handleMutated`
    calls the hook's `reload()` to re-fetch every loaded page (1..N) in place
    so edits stay visible after deep scroll. The footer keeps a running total
    count plus a "loading…" / "— end —" indicator. `useTableData.js` owns the
    page cursor internally and exposes `{ items, total, loading, hasMore,
    loadMore, reload, resetNonce }`.
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
    preferences to their defaults. A separate "Reset Filters" button
    clears only the active filters — the per-column table filters **and**
    the sidebar quick filters (label, modality, study-import label) —
    without touching column visibility/order/sort. It lives in the same
    toolbar portal as "Reset View"; clearing the cross-component sidebar
    filters is delegated to `Web App` via an `onResetSidebarFilters`
    callback passed into `DataTable`.
  - **Default column order**: With no saved `columnOrder` (a clean view, or
    after "Reset View"), built-in data columns come first, followed by label
    columns grouped by instrument (instruments alphabetical, unassigned last)
    and ordered by label creation time (oldest first) within each instrument
    (shared `compareLabelDefsDefault` in `utils/table.js`, also used by the
    sidebar quick-filter list). Any user-saved column order takes precedence
    over this default.
  - **Inline editing with stopPropagation**: Label cells in all table levels
    use `stopPropagation` to prevent expand/collapse when interacting with
    label controls.
  - **Live subtable refresh**: After a successful annotation mutation,
    `handleMutated` re-fetches the main table and all currently expanded child
    and grandchild rows. The edited cell itself updates optimistically (see
    `InlineEdit` below), so this re-fetch is the authoritative reconcile rather
    than the source of the visible change. Note the `*_labelled` mirror columns
    are refreshed in the background after each write, so any labelled-pivot views
    are eventually consistent — but the live table reads `annotations` directly,
    so annotation values are always immediate.
  - **Snapshot refresh**: Authenticated users can trigger a full snapshot
    rebuild from the table summary row.
- `InlineEdit` — handles `bool` (checkbox), `int` (number input), `text`
  (text input), and `select` (pill-style dropdown with search/create) label
  types. Accepts a `level` and generic `entity` prop so it can annotate at
  any level. Annotations are shared across all users (one value per
  entity+label); `created_by` is shown as a tooltip for audit traceability.
  Edits are **optimistic**: `BoolEdit`/`SelectEdit` keep a local `pending`
  override that shows the new value immediately and yields to the reloaded
  `ann` prop once it catches up (a `useEffect` clears the override only when the
  prop matches, avoiding a flicker); `ValueEdit` already renders from local
  input state. On a non-`ok` POST/DELETE the cell rolls back and an `alert`
  fires, and `onMutated` is skipped (nothing new to refresh).
- `ColumnSelector` — dropdown for toggling columns. Labels are grouped by
  their level with headings (Patient labels, Study labels, Series labels).
  Column visibility and order are persisted server-side in
  `user_preferences`.
- `LabelDefModal` — form to create new label definitions with level and
  datatype selectors, including a select-type option builder with colored
  pills
- `PreviewPane` — lower OHIF iframe container with inline loading/error states.
  Its "Open in New Tab" / "Collapse" controls are not overlaid on the iframe;
  they render in the `DataTable` footer as dark-navy tabs (default `#1a2256`,
  hover `#090C29`) centered in a flex slot between the Refresh buttons and the
  row count, visually stemming from the pane's top edge. A mirror flex slot
  keeps the count centered. State is threaded from `Navigator.jsx` into
  `DataTable` via the `previewOpen` / `previewUrl` / `onPreviewClose` props.
  The sidebar toggle (`.sidebar__toggle`) shares the same navy palette/shape.
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

The web app can rebuild three snapshot tables on demand via
`POST /api/snapshots/refresh`. Each snapshot is a `CREATE TABLE ... AS SELECT`
that joins the source table with pivoted annotation columns for that level's
label definitions:

- `snapshot_patients` — from `patient` + patient-level annotations
- `snapshot_studys` — from `image_study` + study-level annotations
- `snapshot_seriess` — from `image_series` + series-level annotations

These tables are dropped and recreated on each refresh and are intended for
bulk export or periodic reporting.

**Table DDL, indexes, and cold-storage columns** are documented in [`data_stores.md`](data_stores.md); migrations are managed via Alembic (`web-app/alembic/`).
