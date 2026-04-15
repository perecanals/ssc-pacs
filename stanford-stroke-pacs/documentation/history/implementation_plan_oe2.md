> **Historical document.** OE2 rollout notes; some names reflect older schema (`flowcat_*`). Current stack: [`../reference/architecture.md`](../reference/architecture.md).

# Implementation Plan: Orthanc Explorer 2 (OE2) Integration

## 1. Context

### What we have
- **Orthanc** running via `orthancteam/orthanc:latest` (Docker, host networking).
- **PostgreSQL** index (`orthanc_db`) — no DICOM file duplication; the Folder Indexer reads
  from a read-only mount at `/dicom-data`.
- **OHIF viewer** plugin active at `/ohif/`.
- **`enrich_orthanc.py`** — patches Orthanc's index tables so the web UI shows
  `nhc`, `proces_id`, and `seriesdescription_` instead of "Anonymous".
- **SSH tunneling** for remote browser access (no reverse proxy).

### What we want
1. **Orthanc Explorer 2** as the default UI — modern study list with search, sort,
   and label-based filtering.
2. **Labels as custom filterable columns** — any user can tag studies with arbitrary
   labels (e.g. `BASAL`, `THROMBECTOMY`, `reviewed`, `pending_annotation`).
   Labels set by one user are visible (and filterable) by all others.
3. **Pre-populated labels** — automatically seed labels from the source
   `flowcat_image_series` table (e.g. `study_type`, `modality`) so users can
   filter immediately after deployment.
4. **OHIF integration preserved** — one-click launch from the OE2 study list.

---

## 2. Implementation Steps

### Step 1: Enable OE2 in `orthanc.json`

The `orthancteam/orthanc:latest` image already ships with the OE2 plugin.
We only need to add a configuration block.

Add to `orthanc.json`:

```json
"OrthancExplorer2": {
    "Enable": true,
    "IsDefaultOrthancUI": true,
    "UiOptions": {
        "EnableEditLabels": true,
        "EnableLabelsCount": true,
        "EnableStudyList": true,
        "StudyListColumns": [
            "PatientID",
            "PatientName",
            "StudyDate",
            "AccessionNumber",
            "StudyDescription",
            "modalities",
            "seriesCount"
        ],
        "EnableOpenInOhifViewer3": true,
        "OhifViewer3PublicRoot": "/ohif/"
    }
}
```

**Key points:**
- `IsDefaultOrthancUI: true` makes OE2 load at the root URL instead of the legacy UI.
- `EnableEditLabels` lets users add/remove labels from the UI.
- `EnableLabelsCount` shows a label sidebar with counts for quick filtering.
- `StudyListColumns` controls what columns appear. Thanks to `enrich_orthanc.py`,
  `PatientID` shows `nhc`, `PatientName` shows `proces_id`, and
  `AccessionNumber` shows `proces_id`.
- `EnableOpenInOhifViewer3` adds a direct OHIF launch button per study.

### Step 2: Create `label_studies.py` — Pre-populate Labels via REST API

Write a Python script that reads `flowcat_image_series` and applies labels to
the corresponding Orthanc studies using the REST API.

**Why REST API instead of direct SQL?**
Labels are managed through Orthanc's internal bookkeeping (not standard DICOM tags).
The REST API (`PUT /studies/{id}/labels/{label}`) is the supported, safe interface.

**Logic:**
1. Connect to the `proces` database, query distinct `(studyinstanceuid, study_type, modality)`.
2. For each study, find the Orthanc study ID by querying
   `POST /tools/lookup` with the StudyInstanceUID.
3. Apply labels:
   - `study_type` value as a label (e.g. `BASAL`, `THROMBECTOMY`, `FOLLOW_UP`, `OTHER`).
   - `modality` as a label (e.g. `CT`, `MR`).
4. Idempotent — PUTting an existing label is a no-op, safe to re-run.

**Usage pattern:**
- Run once after initial deployment to seed labels.
- Re-run whenever new studies are indexed to label them.
- Users can also add custom labels manually through the OE2 UI at any time.

### Step 3: Restart and Verify

1. `docker compose down && docker compose up -d`
2. Verification checklist:
   - Navigate to `http://localhost:8042/` — should load OE2 (not legacy UI).
   - Label sidebar visible on the left with label counts.
   - Select a study → add a test label → filter by it.
   - Click OHIF button → viewer loads correct study.
   - Pre-populated labels (`BASAL`, `CT`, etc.) appear and are filterable.

### Step 4: Update `check_status.sh`

Add an OE2-specific endpoint check (`/ui/app/`) alongside the existing
`/app/explorer.html` check, and verify the labels API responds
(`/studies?labels=...`).

---

## 3. File Changes Summary

| File | Action | Description |
|---|---|---|
| `orthanc.json` | **Modify** | Add `OrthancExplorer2` config block |
| `label_studies.py` | **Create** | Script to pre-populate labels from source DB via REST API |
| `check_status.sh` | **Modify** | Add OE2 endpoint check |
| `requirements.txt` | **Modify** | Add `requests` (needed for label script) |

---

## 4. Multi-user Label Workflow

Once deployed, the label workflow is:

1. **Pre-seeded labels** (from `label_studies.py`): `BASAL`, `THROMBECTOMY`,
   `FOLLOW_UP`, `OTHER`, `CT`, `MR`, etc. — available immediately.
2. **User-created labels**: Any user clicks a study → "Edit labels" → types a new
   label (e.g. `reviewed_by_alice`, `needs_second_opinion`). The label appears
   globally in the sidebar for all users.
3. **Filtering**: Click a label in the sidebar to filter the study list. Combine
   with text search (nhc, proces_id) for precise lookups.
4. **Sharing**: All labels are shared. If User A labels a study `interesting_case`,
   User B sees it in their sidebar and can filter by it.

---

## 5. What We Are NOT Adding (and why)

- **Nginx reverse proxy**: The current SSH tunneling setup works and is already
  secure. Adding Nginx adds complexity with no functional gain unless we need
  public HTTPS access or multiple subdomains. Can be added later if needed.
- **Custom SQL columns in `flowcat_image_series`**: The source table is read-only.
  OE2 Labels provide the equivalent functionality without touching it.
- **User authentication beyond Orthanc's built-in**: OE2 respects
  `RegisteredUsers`. Fine for the current team size. Can be upgraded to
  Keycloak/LDAP later if needed.
