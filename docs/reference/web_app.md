# Stanford Stroke Center Web App App

**Purpose:** Product-level reference for the web app — why it exists, features, and UI model. For stack-wide architecture see [`architecture.md`](architecture.md). For SQL schemas and tables see [`data_stores.md`](data_stores.md). For deep React/component behavior see [`web_app_frontend.md`](web_app_frontend.md).

This document describes the purpose, design rationale, and main features of the
Web App application in the Stanford Stroke Center PACS stack.

---

## 1. Purpose

The Web App is the research-facing web application that sits alongside
Orthanc.

Its job is not to replace Orthanc as a PACS server or DICOM indexer. Instead,
it provides a workflow-oriented interface for:

- browsing patients, studies, and series from the research metadata tables
- creating and editing annotations at patient, study, and series level
- filtering across hierarchy levels using those annotations
- previewing images in OHIF while staying inside the annotation workflow

In short:

- Orthanc is the PACS/viewer infrastructure
- the web app is the annotation and navigation layer for research work

---

## 2. Construction Rationale

Orthanc Explorer 2 handles PACS operations and study-level review but not the
richer SSC annotation workflow, so the Web App adds multi-level annotations
(vs study-only labeling), research-oriented cross-level filtering, inherited
annotation visibility, and a table-centric surface where review and annotation
happen together.

Key design choices:

- **React + FastAPI + Vite** — FastAPI serves the REST API, auth, and the
  pre-built frontend as static files; React drives the dynamic table state,
  nesting, filtering, and inline editing. Production stays one native service on
  `:8043` with no Node process.
- **One generic `DataTable`** for patients, studies, and series — keeps sorting,
  filtering, column visibility, annotation rendering, and row expansion
  consistent across levels and avoids duplicate table implementations.
- **Embedded OHIF preview pane** (vs open-in-new-tab only) — lets users review
  a study/series row's images without leaving the annotation workflow.

---

## 3. High-Level Architecture

The Web App is one native host service running on port `8043`.

It has two main parts:

- **Backend**: `web-app/app.py` (entry point), `web-app/routes/` (API routers), `web-app/db.py` (connection pool), `web-app/auth.py` (JWT), `web-app/common.py` (shared SQL helpers)
- **Frontend**: `web-app/src/`

At runtime:

1. the browser loads the Navigator UI from FastAPI
2. the frontend requests metadata and annotations from Web App API endpoints
3. the backend reads source rows from `patient` (patient tab, joined to
   `lvo_clinical_data` for the clinical `stroke_date`), `image_study`, and
   `image_series`
4. the backend reads/writes web-app-owned tables such as `annotations`,
   `label_definitions`, and `users`
5. when the user requests image viewing, the backend resolves an OHIF URL via
   Orthanc and returns it to the frontend
6. the frontend either embeds that URL in the lower preview pane or opens it in
   a separate tab

---

## 4. Main UI Structure

The current Navigator UI is organized into four main areas.

### 4.1 Top bar

The top bar contains:

- home link / app identity
- level switcher for `Patients`, `Studies`, and `Series`
- column selector
- Reset Filters button (clears column + sidebar quick filters only)
- Reset View button (restores all table preferences to defaults)
- new label type trigger
- login/logout controls

The level switcher lives here to save vertical space for the main workflow.

### 4.2 Sidebar

The sidebar contains:

- search
- annotation labels grouped by level
- quick modality filters for study/series contexts

Its role is global filtering, not row-level editing.

### 4.3 Main data table

The main table is the core interaction surface.

Key behaviors:

- patient rows can expand into studies
- study rows can expand into series
- nested rows support inherited annotations
- columns can include both source metadata and annotation-driven fields
- inline editors allow direct annotation changes in the table
- the machine classifier's verdicts appear as read-only **Auto Series Type** and
  **Auto Timepoint** columns, rendered as muted outlined pills next to — and
  deliberately distinct from — the editable human labels of the same name; they
  are an independent axis, never derived from each other (see §5.7)
- the selected study or series row remains highlighted
- column headers are sticky during vertical scroll
- the first column can be pinned (frozen) during horizontal scroll via a
  toggle in the header
- columns can be reordered by dragging and dropping headers
- all table preferences (column visibility, order, sort, filters, frozen
  state) are persisted per user on the server in the `user_preferences`
  table; preferences are loaded on mount and saved automatically
- the current hierarchy level and the sidebar quick filters are also
  persisted per user (under the `_global` preferences level) and restored
  on the next visit, so a session resumes where it left off
- the Actions column is hidden at the patient level (no OHIF action applies)

For component-level detail (routes, `DataTable` internals, etc.), see
[`web_app_frontend.md`](web_app_frontend.md).

### 4.4 Embedded OHIF preview pane

The lower preview pane appears when an active study or series is selected.

It:

- embeds OHIF in an `iframe`
- can be collapsed
- includes overlay controls for opening the same viewer in a new tab
- uses study URLs for study selections
- uses series-specific OHIF URLs for series selections

This pane is intentionally part of the Navigator page rather than a modal, so
the table remains visible while images are reviewed.

---

## 5. Main Features

### 5.1 Multi-level annotation

The Web App supports:

- patient-level annotations
- study-level annotations
- series-level annotations

Label definitions are also level-aware, so the same system can be reused at all
three hierarchy levels.

### 5.2 Cross-level filtering

The UI can filter one level by annotations defined on another.

Examples:

- filter patients by a series-level label
- filter studies by a patient-level label
- filter series by a study-level label

This is one of the main reasons the web app is more useful than a
viewer-only UI for research tasks.

### 5.3 Inherited annotation visibility

Child rows surface parent-level annotations as inherited values.

That means:

- a study can show annotations inherited from its patient
- a series can show annotations inherited from its study and patient

This keeps the hierarchy understandable without duplicating data entry.

### 5.4 Inline editing

Annotations are edited directly in table cells. Annotations are **shared
across all users**: there is one value per entity+label, and any
authenticated user can edit it (last-write-wins). The `created_by` field
tracks who last modified each annotation and is shown as a tooltip.

Edits apply **optimistically**: the cell updates instantly on click and the
write is sent in the background, so editing never waits on the round-trip. If
the save fails the cell rolls back to its previous value and an alert is shown.
The per-level `*_labelled` mirror tables are refreshed in the background after
each write (they are eventually consistent, not synchronous), but nothing in
the live table read path depends on them — the table reads `annotations`
directly, so edits are always reflected immediately.

Supported data types currently include:

- `bool`
- `int`
- `text`
- `select`

This keeps the workflow fast and avoids modal-heavy editing for routine labels.

### 5.5 Column customization

Users can control visible columns per level.

These preferences are stored server-side in the `user_preferences` table
(per user and per level), so the table layout persists across browsers,
devices, and sessions. Preferences include column visibility, column order,
sort column/direction, column filters, and frozen-column state. A separate
`_global` row stores the Navigator session state — the last-used hierarchy
level and the sidebar quick filters — which is restored on the next login.
Clearing filters (including via "Reset Filters") persists the cleared state
the same way.

Column display order can be changed by dragging and dropping column headers.
A "Reset View" button in the top bar restores all table preferences to
their defaults. A "Reset Filters" button next to it clears only the
active filters — both the per-column table filters and the sidebar quick
filters — leaving column visibility, order, and sort untouched.

When a label is selected from the sidebar, that label column is also forced
visible in the active table display for as long as the filter is active. If the
column was not already part of the user's saved column selection, it hides again
when the sidebar label is cleared.

### 5.6 Per-user dataset access

Every non-admin user has a dataset scope (`users.allowed_datasets`, a subset
of the `patient.dataset` cohort tags such as `PRECISE` / `CRISP2/LVO`) that
gates what they see:

- list endpoints return only in-scope patients (and their studies/series);
  the sidebar's Dataset and Import-label option lists narrow the same way
- detail endpoints keyed by an entity id return **404** for out-of-scope ids;
  the DICOMweb proxy 403s out-of-scope studies; deny-by-default (no grants =
  empty table), and admins bypass
- admins manage grants in the **`/admin` page** (users × datasets checkbox grid)
  or via `scripts/admin/manage_users.py set-datasets`; `/api/me` exposes
  `allowed_datasets` to the SPA

The exact enforcement points, TTL caches, and the `/api/labels*` known
limitation are canonical in [`architecture.md`](architecture.md) §5.4.

### 5.7 Machine classification ("Auto" columns)

The ingestion pipeline classifies every series (`image_series.series_type`) and
every study (`image_study.timepoint`). The web app shows those verdicts read-only
as **Auto Series Type** and **Auto Timepoint**, and lets users filter and sort on
them — but never writes them.

The point of the "Auto" prefix and the muted, outlined, non-clickable pill is
that annotation labels named `series_type` and `timepoint` also exist and sit in
adjacent columns. The two are **independent axes**: a reclassify run must not
overwrite a rater's judgement, and a rater's judgement must not feed back into
the rules ([`data_stores.md`](data_stores.md)).

- **Auto Series Type** also shows the per-patient preference rank as a
  superscript: `NCCT` with a bold `1` is *the* NCCT to open for that patient.
  Type the label into the column filter (`NCCT_1`) to isolate every patient's
  preferred NCCT in one query.
- Most series are **excluded** rather than typed (bone, topogram, RAPID output,
  …), which the classifier records as a rule. Those cells show a faint `—`;
  hover it for the exclusion that fired. An empty cell means not-yet-classified.
- **Auto Timepoint** (`BL` / `THROMBECTOMY` / `FU`) is anchored on the
  femoral-sheath puncture. Where no puncture time was recorded the classifier
  falls back to a fixed offset, and those pills are drawn with a dashed border
  and a `~` — an estimate, not a measurement. Hover any Auto pill for its
  provenance (the rule that fired, or the anchor and signed hours from it).
- Both are on by default, including for users with existing saved column
  preferences; hiding one is remembered. The exception: when series appear as
  **sub-rows** under a study or patient, Auto Timepoint is hidden by default —
  the parent study row already shows it, so repeating it per child series adds
  nothing. It defaults on in the flat series table, where there is no parent row
  to carry it, and stays available in the column selector everywhere.

**Sidebar quick filters.** An *Auto Classification* section offers both as
multi-select value pickers (the same popup the select-type annotation labels
use), with the vocabulary and counts read live from the data. Ticking several
values ORs them — NCCT *or* CTA. The two filters AND together, and both apply at
**every** level: at study and patient level they become "has one", so you can ask
for *patients who have an NCCT_1 series and a THROMBECTOMY study* and get the
patient list back. A sidebar pick widens (rather than replaces) any column-header
filter already set on the same field.

These Auto filters — and the select-value annotation-label sidebar filters —
also **cascade into the expanded subtables**, so a drilled-in row shows only the
children that match. Picking a study-level value (e.g. Auto Timepoint *BL*) shows
only the BL studies under an expanded patient; picking a series-level value (e.g.
Auto Series Type *CTA*) shows only the studies that **have** a CTA series and,
within each, only the CTA series. The subtable fetches (`/api/patients/{id}/studies`,
`/api/studies/{uid}/series`) carry the same `series_type` / `timepoint` /
`label_filters` params as the flat browsing endpoints and reuse the same match
logic, so a subtable stays consistent with the top-level list. Changing a
cascading filter drops cached expansion state so the next expand refetches.

---

## 6. OHIF Integration Behavior

The Web App does not talk directly to DICOMweb from the browser. Instead, it
asks its backend to construct OHIF URLs after validating the requested study or
series against local metadata and Orthanc lookup.

Current behavior:

- clicking a study row previews the study
- clicking a series row previews a series-specific OHIF URL
- study and series rows also expose explicit `OHIF` buttons for opening a
  separate tab

Important implementation nuance:

- because the preview pane uses an `iframe`, changing to a different study or
  series currently reloads the embedded OHIF app
- this is acceptable for now, but a deeper integration would be needed for
  seamless in-place series switching without iframe reloads

In **cold storage** mode, the frontend may warm the study cache before opening
OHIF; see [`../cold_storage/design.md`](../cold_storage/design.md).

---

## 7. What the web app Is Not

The Web App is not:

- a DICOM storage server
- a replacement for Orthanc indexing
- the owner of the source imaging files
- the source of truth for the upstream `image_study` / `image_series` metadata

Its responsibility is workflow, annotation, and review support.

---

## 8. Relevant Files

To avoid a second drifting copy, the module inventory lives in one place each:

- **Backend module responsibilities** (`app.py`, `routes/`, `db.py`, `auth.py`,
  `dataset_access.py`, `common.py`, `orthanc_client.py`, `cache_manager.py`,
  `reconciliation.py`, `rate_limit.py`, `labelled_table_sync.py`): see the
  Architecture section of the repo `CLAUDE.md` (one line per module) and
  [`architecture.md`](architecture.md).
- **Frontend routes, components, and `DataTable` internals**: see
  [`web_app_frontend.md`](web_app_frontend.md).

---

## 9. Practical Summary

If you need a one-sentence mental model:

The Web App is a hierarchical annotation browser for patients, studies, and
series, designed to keep metadata review, labeling, filtering, and OHIF image
inspection in one page.
