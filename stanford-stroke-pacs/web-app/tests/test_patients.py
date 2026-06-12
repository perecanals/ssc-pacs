"""Tests for the patient-level listing sourced from the `patient` registry.

Regression coverage for the bug where patients with imaging but no
lvo_clinical_data row were invisible at the patient level, plus the
clinical-preferred / imaging-fallback stroke_date behavior.
"""


def _find(items, patient_id):
    for it in items:
        if it["patient_id"] == patient_id:
            return it
    return None


class TestPatientListing:
    def test_clinically_unmatched_patient_appears(self, logged_in_client):
        """P-0002 has imaging but no lvo_clinical_data row — must be listed."""
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0002"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        row = _find(items, "P-0002")
        assert row is not None, "imaging-only patient missing from /api/patients"

    def test_unmatched_patient_uses_imaging_stroke_date(self, logged_in_client):
        """With no clinical row, stroke_date falls back to earliest study date."""
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0002"})
        row = _find(resp.json()["items"], "P-0002")
        assert str(row["stroke_date"]).startswith("2024-03-03")

    def test_matched_patient_prefers_clinical_stroke_date(self, logged_in_client):
        """P-0001's clinical date (2025-01-01) wins over its imaging date (2025-02-02)."""
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"})
        row = _find(resp.json()["items"], "P-0001")
        assert row is not None
        assert str(row["stroke_date"]).startswith("2025-01-01")

    def test_datasets_endpoint_lists_distinct_tags(self, logged_in_client):
        """/api/datasets returns the distinct, sorted cohort tags across patients."""
        resp = logged_in_client.get("/api/datasets")
        assert resp.status_code == 200
        tags = resp.json()
        # P-0001 is in {lvo, crisp2}, P-0002 in {lvo} → distinct union, sorted.
        assert tags == ["crisp2", "lvo"]

    def test_dataset_filter_narrows_results(self, logged_in_client):
        """dataset=crisp2 isolates P-0001 (member); P-0002 is excluded."""
        resp = logged_in_client.get("/api/patients", params={"dataset": "crisp2"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert _find(items, "P-0001") is not None
        assert _find(items, "P-0002") is None

    def test_patient_row_exposes_dataset_column(self, logged_in_client):
        """The patient row carries `dataset` as a comma-joined string for display."""
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"})
        row = _find(resp.json()["items"], "P-0001")
        assert row is not None
        # text[] {lvo,crisp2} is returned array_to_string-joined for the table cell.
        assert row["dataset"] == "lvo, crisp2"

    def test_dataset_filter_shared_tag_keeps_both(self, logged_in_client):
        """dataset=lvo is a member of both patients' arrays → both listed."""
        resp = logged_in_client.get("/api/patients", params={"dataset": "lvo"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert _find(items, "P-0001") is not None
        assert _find(items, "P-0002") is not None

    def test_studies_and_series_expose_dataset_column(self, logged_in_client):
        """Study/series rows carry the owning patient's comma-joined dataset."""
        studies = logged_in_client.get("/api/studies").json()["items"]
        by_uid = {s["studyinstanceuid"]: s for s in studies}
        assert by_uid["1.2.3.4.5"]["dataset"] == "lvo, crisp2"
        assert by_uid["2.2.2.2.2"]["dataset"] == "lvo"

        series = logged_in_client.get("/api/series").json()["series"]
        assert any(s["dataset"] == "lvo, crisp2" for s in series)

        sub_rows = logged_in_client.get("/api/patients/P-0001/studies").json()
        assert sub_rows[0]["dataset"] == "lvo, crisp2"
        grand_rows = logged_in_client.get("/api/studies/1.2.3.4.5/series").json()
        assert grand_rows[0]["dataset"] == "lvo, crisp2"

    def test_studies_and_series_dataset_filter(self, logged_in_client):
        """dataset=crisp2 keeps P-0001's study/series and drops P-0002's."""
        studies = logged_in_client.get(
            "/api/studies", params={"dataset": "crisp2"}
        ).json()["items"]
        uids = {s["studyinstanceuid"] for s in studies}
        assert "1.2.3.4.5" in uids
        assert "2.2.2.2.2" not in uids

        series = logged_in_client.get(
            "/api/series", params={"dataset": "crisp2"}
        ).json()["series"]
        assert series and all(s["patient_id"] == "P-0001" for s in series)

    def test_sort_by_stroke_date(self, logged_in_client):
        """Sorting by stroke_date orders on the displayed COALESCE value."""
        resp = logged_in_client.get(
            "/api/patients", params={"sort_by": "stroke_date", "sort_dir": "asc"}
        )
        assert resp.status_code == 200
        dates = [
            str(it["stroke_date"]) for it in resp.json()["items"]
            if it["stroke_date"] is not None
        ]
        assert dates == sorted(dates)


# The exact ON CONFLICT clause used by ImageIntegrationProtocol._upsert_patient.
# Kept in sync with image_integration_protocol.py; psycopg2 paramstyle.
_UPSERT_SQL = """
INSERT INTO patient (patient_id, stroke_date, import_id, import_label, dataset,
                     created_at, updated_at)
SELECT s.patient_id, MIN(s.acquisitiondatetime),
       %(import_id)s, %(import_label)s, %(dataset)s, now(), now()
FROM image_study s
WHERE s.patient_id = ANY(%(patient_ids)s)
GROUP BY s.patient_id
ON CONFLICT (patient_id) DO UPDATE SET
  stroke_date = EXCLUDED.stroke_date,
  dataset = ARRAY(SELECT DISTINCT unnest(patient.dataset || EXCLUDED.dataset) ORDER BY 1),
  updated_at = now()
"""


class TestPatientUpsertSemantics:
    """Mirrors ImageIntegrationProtocol._upsert_patient: origin-preserving
    import provenance, deduped dataset array union, recomputed stroke_date."""

    PID = "PT-UPSERT"

    def _upsert(self, cur, *, import_id, import_label, dataset):
        cur.execute(_UPSERT_SQL, {
            "import_id": import_id,
            "import_label": import_label,
            "dataset": dataset,
            "patient_ids": [self.PID],
        })

    def test_origin_preserved_union_and_min(self, db_conn):
        cur = db_conn.cursor()
        # Batch 1: one study, import_id 10.
        cur.execute(
            "INSERT INTO image_study (patient_id, studyinstanceuid, acquisitiondatetime, "
            "import_id, import_label) VALUES (%s, 'up.1', '2025-05-05', 10, 'b1')",
            (self.PID,),
        )
        self._upsert(cur, import_id=10, import_label="b1", dataset=["ds1"])
        cur.execute(
            "SELECT stroke_date, import_id, import_label, dataset FROM patient WHERE patient_id=%s",
            (self.PID,),
        )
        stroke, iid, ilabel, dataset = cur.fetchone()
        assert str(stroke).startswith("2025-05-05")
        assert (iid, ilabel, dataset) == (10, "b1", ["ds1"])

        # Batch 2: earlier study, import_id 20, new dataset/label.
        cur.execute(
            "INSERT INTO image_study (patient_id, studyinstanceuid, acquisitiondatetime, "
            "import_id, import_label) VALUES (%s, 'up.2', '2025-01-01', 20, 'b2')",
            (self.PID,),
        )
        self._upsert(cur, import_id=20, import_label="b2", dataset=["ds2"])
        cur.execute(
            "SELECT stroke_date, import_id, import_label, dataset FROM patient WHERE patient_id=%s",
            (self.PID,),
        )
        stroke, iid, ilabel, dataset = cur.fetchone()
        assert str(stroke).startswith("2025-01-01")   # recomputed global MIN
        assert (iid, ilabel) == (10, "b1")             # ORIGIN preserved
        assert dataset == ["ds1", "ds2"]               # deduped union, ordered

        # Idempotent re-run: dataset stays deduped, origin unchanged.
        self._upsert(cur, import_id=20, import_label="b2", dataset=["ds2"])
        cur.execute("SELECT import_id, dataset FROM patient WHERE patient_id=%s", (self.PID,))
        iid, dataset = cur.fetchone()
        assert (iid, dataset) == (10, ["ds1", "ds2"])
        # db_conn fixture rolls back — no committed test rows to clean up.
