> **Historical document.** Original companion plan (Docker, `proces` DB, `flowcat_*`). Current Companion: [`../reference/companion.md`](../reference/companion.md) and [`../reference/runtime_and_config.md`](../reference/runtime_and_config.md).

# Implementation Plan: Series Annotation Companion App

## 1. Context

### Problem
OE2 provides study-level label filtering, but researchers need to tag and filter
at the **series** level (e.g. "this DWI series shows a lacunar infarct"). OE2's
UI cannot be extended to support this — it is a pre-compiled plugin.

### Solution
A self-contained FastAPI companion app that:
- Stores series-level annotations in a `flowcat_annotations` table (in the `proces` DB)
- Provides a web UI for browsing, tagging, and filtering series
- Links back to OHIF for viewing
- Runs as an independent Docker container, removable with zero impact on Orthanc

---

## 2. Architecture

```
Browser
├── OE2 (:8042/ui/app/)        ← study-level browsing & labels
├── Companion (:8043)           ← series-level browsing & annotations
└── OHIF (:8042/ohif/)          ← image viewing

Docker
├── Orthanc container (:8042)   ← unchanged
└── Companion container (:8043) ← new, FastAPI

PostgreSQL (proces DB)
├── flowcat_image_series        ← read-only source data
└── flowcat_annotations         ← new, read-write annotation store
```

## 3. Components

### `flowcat_annotations` table
- Auto-created by the FastAPI app on startup
- Schema: seriesinstanceuid, studyinstanceuid, nhc, label, created_by, created_at, notes
- UNIQUE constraint on (seriesinstanceuid, label, created_by)
- Indexed on label and seriesinstanceuid

### `companion/app.py` — FastAPI backend
- `GET /api/series` — paginated series list with filters (label, nhc, modality, description)
- `GET /api/series/{uid}/annotations` — annotations for a series
- `POST /api/annotations` — create annotation
- `DELETE /api/annotations/{id}` — remove annotation
- `GET /api/labels` — distinct labels
- `GET /api/labels/summary` — label counts
- Series data from `flowcat_image_series` LEFT JOIN `flowcat_annotations`
- Orthanc API only for "Open in OHIF" link resolution

### `companion/static/index.html` — UI
- Label sidebar with counts and text search
- Sortable series table (NHC, Proces ID, Study Type, Modality, Series Description, Labels)
- Inline label add/remove, "Open in OHIF" links
- Vanilla HTML/JS/CSS, no build step

### Docker
- `companion/Dockerfile` — python:3.11-slim + uvicorn
- `companion/requirements.txt` — fastapi, uvicorn, psycopg2-binary, requests
- Added as `companion` service in `docker-compose.yml` with `network_mode: host`

## 4. Modularity

To remove the companion app entirely:
1. Delete the `companion/` folder
2. Remove the `companion` service from `docker-compose.yml`
3. (Optional) `DROP TABLE flowcat_annotations;`

No other files are affected.
