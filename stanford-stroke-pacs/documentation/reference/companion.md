# Stanford Stroke Center Companion App

**Purpose:** Product-level reference for the Companion — why it exists, features, and UI model. For stack-wide architecture see [`architecture.md`](architecture.md). For SQL schemas and tables see [`data_stores.md`](data_stores.md). For deep React/component behavior see [`companion_frontend.md`](companion_frontend.md).

This document describes the purpose, design rationale, and main features of the
Companion application in the Stanford Stroke Center PACS stack.

---

## 1. Purpose

The Companion is the research-facing web application that sits alongside
Orthanc.

Its job is not to replace Orthanc as a PACS server or DICOM indexer. Instead,
it provides a workflow-oriented interface for:

- browsing patients, studies, and series from the research metadata tables
- creating and editing annotations at patient, study, and series level
- filtering across hierarchy levels using those annotations
- previewing images in OHIF while staying inside the annotation workflow

In short:

- Orthanc is the PACS/viewer infrastructure
- the Companion is the annotation and navigation layer for research work

---

## 2. Construction Rationale

### 2.1 Why the Companion exists

Orthanc Explorer 2 is useful for PACS operations and study-level review, but it
does not provide the richer workflow needed for the SSC annotation use case.

The Companion was built to support:

- multi-level annotations instead of study-only labeling
- research-oriented filtering across patients, studies, and series
- inherited annotation visibility from parent rows
- a table-centric workflow where image review and annotation happen together

### 2.2 Why React + FastAPI

The app uses:

- **FastAPI** for the REST API, auth, and static-file serving
- **React** for the interactive browser UI
- **Vite** for frontend development and production builds

This split was chosen because:

- the backend needs straightforward database and auth logic
- the frontend needs dynamic table state, nesting, filtering, and inline editing
- the production deployment should stay simple: one native service on port
  `8043`, no frontend Node process

### 2.3 Why a generic hierarchical table

Instead of separate screens or separate table implementations, the Companion
uses one configurable `DataTable` component for patients, studies, and series.

This keeps behavior consistent across levels:

- sorting
- filtering
- column visibility
- annotation rendering
- expansion into child rows

It also reduces duplication for future feature work.

### 2.4 Why embed OHIF

Originally, OHIF integration was mainly "open in new tab."

The embedded preview pane was added so users can:

- click a study or series row
- review images without leaving the Companion
- continue annotating with the relevant row context still visible

This improves the workflow significantly for rapid review and labeling.

---

## 3. High-Level Architecture

The Companion is one native host service running on port `8043`.

It has two main parts:

- **Backend**: `companion/app.py` (entry point), `companion/routes/` (API routers), `companion/db.py` (connection pool), `companion/auth.py` (JWT), `companion/common.py` (shared SQL helpers)
- **Frontend**: `companion/src/`

At runtime:

1. the browser loads the Companion UI from FastAPI
2. the frontend requests metadata and annotations from Companion API endpoints
3. the backend reads source rows from `lvo_clinical_data`, `image_study`, and
   `image_series`
4. the backend reads/writes Companion-owned tables such as `annotations`,
   `label_definitions`, and `users`
5. when the user requests image viewing, the backend resolves an OHIF URL via
   Orthanc and returns it to the frontend
6. the frontend either embeds that URL in the lower preview pane or opens it in
   a separate tab

---

## 4. Main UI Structure

The current Companion UI is organized into four main areas.

### 4.1 Top bar

The top bar contains:

- home link / app identity
- level switcher for `Patients`, `Studies`, and `Series`
- column selector
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
- the selected study or series row remains highlighted
- column headers are sticky during vertical scroll
- the first column can be pinned (frozen) during horizontal scroll via a
  toggle in the header
- columns can be reordered by dragging and dropping headers
- all table preferences (column visibility, order, sort, filters, frozen
  state) are persisted per user on the server in the `user_preferences`
  table; preferences are loaded on mount and saved automatically
- the Actions column is hidden at the patient level (no OHIF action applies)

For component-level detail (routes, `DataTable` internals, etc.), see
[`companion_frontend.md`](companion_frontend.md).

### 4.4 Embedded OHIF preview pane

The lower preview pane appears when an active study or series is selected.

It:

- embeds OHIF in an `iframe`
- can be collapsed
- includes overlay controls for opening the same viewer in a new tab
- uses study URLs for study selections
- uses series-specific OHIF URLs for series selections

This pane is intentionally part of the Companion page rather than a modal, so
the table remains visible while images are reviewed.

---

## 5. Main Features

### 5.1 Multi-level annotation

The Companion supports:

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

This is one of the main reasons the Companion is more useful than a
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
sort column/direction, column filters, and frozen-column state.

Column display order can be changed by dragging and dropping column headers.
A "Reset View" button in the top bar restores all table preferences to
their defaults.

When a label is selected from the sidebar, that label column is also forced
visible in the active table display for as long as the filter is active. If the
column was not already part of the user's saved column selection, it hides again
when the sidebar label is cleared.

### 5.6 Snapshot refresh

Authenticated users can rebuild the snapshot tables used for export/reporting.

This gives the app an explicit "refresh derived reporting state" action without
mixing that logic into routine browsing.

---

## 6. OHIF Integration Behavior

The Companion does not talk directly to DICOMweb from the browser. Instead, it
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

## 7. What the Companion Is Not

The Companion is not:

- a DICOM storage server
- a replacement for Orthanc indexing
- the owner of the source imaging files
- the source of truth for the upstream `image_study` / `image_series` metadata

Its responsibility is workflow, annotation, and review support.

---

## 8. Relevant Files

The most important Companion files are:

- `companion/app.py` - FastAPI entry point (lifespan, middleware, router registration)
- `companion/routes/` - API route modules (auth, studies, annotations, labels, cold_storage, admin, preferences, static)
- `companion/db.py` - DB connection pool and `DB_CONFIG` (single source of truth)
- `companion/auth.py` - JWT utilities and auth dependencies
- `companion/common.py` - shared label-filter SQL builders and annotation helpers
- `companion/orthanc_client.py` - Orthanc REST API wrappers
- `companion/cache_manager.py` - cold-storage warm/evict logic
- `companion/src/pages/Companion.jsx` - page-level layout and preview state
- `companion/src/components/DataTable.jsx` - hierarchical table logic
- `companion/src/components/PreviewPane.jsx` - embedded OHIF pane
- `companion/src/components/TopBar.jsx` - top navigation and controls host
- `companion/src/components/Sidebar.jsx` - global filters and labels
- `companion/src/components/InlineEdit.jsx` - in-table annotation editing
- `companion/src/components/ColumnSelector.jsx` - column visibility and order control
- `companion/src/components/LabelDefModal.jsx` - label-definition creation UI

---

## 9. Practical Summary

If you need a one-sentence mental model:

The Companion is a hierarchical annotation browser for patients, studies, and
series, designed to keep metadata review, labeling, filtering, and OHIF image
inspection in one page.
