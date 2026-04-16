# Workstream 11 — Data integrity: two-DB reconciliation

**Status:** `todo`
**Priority:** `P2`
**Size:** `M` (≈ 3–5 days)
**Owner:** _(unassigned)_
**Dependencies:** WS 06 (observability — reuses logging/metrics)

---

## 1. Context

The PACS has two PostgreSQL databases that must stay in sync but have no
enforced referential integrity:

- **`orthanc_db`** — owned by Orthanc; indexes DICOM files on disk.
- **`stanford-stroke`** — owned by the Companion; `image_series` tracks
  research metadata including `dicom_dir_path` and `dicom_archive_path`.

If a series exists in `image_series` but not in Orthanc's index (or vice
versa), the UI can link users to OHIF URLs that 404, or offer series
for warming that don't actually have archives. Cold-storage evictions
can fail mid-way and leave an `image_series` row pointing at a path
Orthanc no longer knows about.

Today the only way to detect a mismatch is for a user to click and
complain. This workstream adds a reconciliation job that diffs the two
sources continuously and surfaces drift.

See `AUDIT_FINDINGS.md` §4.2.

---

## 2. Scope

**In scope:**
- A read-only reconciliation script and library that compares:
  - series in `image_series` vs. Orthanc's index
  - `dicom_archive_path` on disk vs. DB column
  - `dicom_dir_path` on disk vs. cache_state
- An admin-only endpoint `/api/admin/reconciliation` exposing the
  latest report.
- A scheduled systemd timer to run reconciliation and emit metrics.
- A documented procedure for fixing common mismatch classes.

**Out of scope:**
- Automatic repair of mismatches (this is a read-only observer —
  repair belongs in targeted scripts with explicit human approval).
- Adding DB-level foreign keys between the two logical DBs (not
  possible with two distinct databases anyway).
- Reconciling `lvo_clinical_data` against `image_study` (upstream
  concern).

---

## 3. Findings

- **F-11.1** — No cross-DB reconciliation job; drift is invisible.
- **F-11.2** — Eviction failure modes (addressed in WS 05) can leave
  `cache_state` stale vs. disk.
- **F-11.3** — Ingest path (`image_integration_protocols/`) can fail
  partway and leave rows in `image_series` with `dicom_archive_path =
  NULL`. A related script (`scripts/cold_storage/list_unarchived_series.py`) already
  exists for one dimension.
- **F-11.4** — `verify_indexing.py` at the top level compares
  `image_series` to Orthanc's REST API. Good starting point — can be
  generalized and integrated.

---

## 4. Tasks

- [ ] **T1** — Inventory the existing verification scripts:
  - `verify_indexing.py` — compares `image_series` rows with Orthanc REST
    /tools/find-series-by-uid.
  - `scripts/cold_storage/list_unarchived_series.py` — flags series with no
    `dicom_archive_path`.
  - Any others. Read them; list in this workstream file for reference.
- [ ] **T2** — Design a unified reconciliation module at
  `stanford-stroke-pacs/companion/reconciliation.py` (new) with
  functions:
  - `diff_image_series_vs_orthanc()` → list of mismatches by category
    (`in_db_not_in_orthanc`, `in_orthanc_not_in_db`,
    `dicom_dir_missing`, `dicom_archive_missing`).
  - `snapshot_summary()` → counts per category, suitable for metrics.
- [ ] **T3** — Add a CLI wrapper:
  `stanford-stroke-pacs/scripts/data_integrity/reconcile.py` (new) that prints a
  human-readable summary and writes a JSON report to
  `maintenance/reconciliation-reports/YYYY-MM-DDTHHMMSS.json`.
  (Reports are intentionally under `maintenance/` so they're
  co-located with ops state, not application state.)
- [ ] **T4** — Add `GET /api/admin/reconciliation/latest` endpoint in
  `companion/routes/admin.py` (if WS 09 landed) or `app.py` (if not).
  Returns the most recent JSON report. Admin-only — verified against
  the `users.is_admin` flag.
- [ ] **T5** — Add Prometheus gauges (depends on WS 06):
  - `reconciliation_mismatches_total{category}` (gauge, not counter —
    it's a snapshot)
  - `reconciliation_last_run_timestamp`
  - `reconciliation_duration_seconds`
  Refresh these on every reconciliation run.
- [ ] **T6** — Wire a systemd timer
  (`stanford-stroke-pacs/systemd/reconciliation.{service,timer}`) to run
  `scripts/data_integrity/reconcile.py` every 6 hours. Emits metrics; stores reports.
- [ ] **T7** — Write
  `documentation/operations/reconciliation.md` (new) covering:
  - what each mismatch category means,
  - the procedure for investigating and manually repairing each
    category (pointer to existing scripts where relevant),
  - the admin-endpoint URL.
- [ ] **T8** — Retire `verify_indexing.py` by re-exporting its behavior
  from `reconciliation.py`, or deprecate it with a note pointing to
  the new module.
- [ ] **T9** — Seed a deliberate mismatch in a test DB (e.g. insert a
  fake `image_series` row with a non-existent UID) and verify
  `reconcile.py` surfaces it.

---

## 5. Acceptance criteria

- [ ] `python scripts/data_integrity/reconcile.py --json` produces a valid JSON
  report on a healthy system (empty mismatch lists).
- [ ] Seeding a mismatch surfaces it in the next run's report and in
  Prometheus.
- [ ] `/api/admin/reconciliation/latest` returns the most recent
  report; non-admin users get 403.
- [ ] `journalctl -u reconciliation.service` shows a successful run
  within the last 6 hours.
- [ ] `documentation/operations/reconciliation.md` exists and is
  linked from `documentation/context.md`.

---

## 6. Verification

```bash
# CLI run
cd stanford-stroke-pacs
python scripts/data_integrity/reconcile.py

# JSON output
python scripts/data_integrity/reconcile.py --json | jq '.summary'

# API (as admin)
curl -b cookies.txt http://localhost:8043/api/admin/reconciliation/latest | jq .

# Timer
systemctl list-timers reconciliation.timer
journalctl -u reconciliation.service -n 50

# Seed + detect
psql -d stanford-stroke -c \
  "INSERT INTO image_series (series_uid, study_uid, dicom_dir_path) VALUES
   ('test-uid-not-in-orthanc','test-study','/nonexistent') ON CONFLICT DO NOTHING;"
python scripts/data_integrity/reconcile.py | grep 'test-uid-not-in-orthanc'
psql -d stanford-stroke -c \
  "DELETE FROM image_series WHERE series_uid = 'test-uid-not-in-orthanc';"
```

---

## 7. Rollback

Read-only workstream. Disable the timer and delete the new endpoint
to fully revert:

```bash
sudo systemctl disable --now reconciliation.timer
```

The reports directory can stay (inert).

---

## 8. Files touched

- `stanford-stroke-pacs/companion/reconciliation.py` (new)
- `stanford-stroke-pacs/companion/routes/admin.py` (edit if WS 09
  landed; otherwise new)
- `stanford-stroke-pacs/scripts/data_integrity/reconcile.py` (new)
- `stanford-stroke-pacs/systemd/reconciliation.service` (new)
- `stanford-stroke-pacs/systemd/reconciliation.timer` (new)
- `stanford-stroke-pacs/documentation/operations/reconciliation.md`
  (new)
- `stanford-stroke-pacs/documentation/context.md` (edit — add link)
- `verify_indexing.py` (top level — edit or deprecate)
- `maintenance/reconciliation-reports/` (new, empty, with a `.gitkeep`)

---

## 9. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Reconciliation scan hammers Orthanc REST | med | low | Batch queries; respect pagination; cap QPS |
| Large series count → long scan time | med | low | Make it incremental (process N per run) if runtime exceeds timer cadence |
| False positives due to concurrent warm/evict | high | low | Re-check mismatches across two consecutive runs before flagging in the metric |
| Admin endpoint leaks data | low | med | Verify admin check; no PHI in report beyond series UIDs |
| Reports directory grows without bound | med | low | Retain last N reports only (rotate in the script) |

---

## 10. Notes

- This is an **observer**, not a repair tool. Resist the temptation to
  auto-fix; a false-positive auto-repair is catastrophic. Emit metrics
  and alerts, let humans trigger repair scripts.
- The ingest pipeline at `image_integration_protocols/` is the biggest
  source of NULL `dicom_archive_path` rows. That's known and operators
  already retry via `scripts/cold_storage/archive_all_series.py --patient <id>`.
  Document that connection in `reconciliation.md`.
- If WS 06 is not yet landed, the metrics tasks (T5) can be skipped;
  keep the JSON report and the API.
