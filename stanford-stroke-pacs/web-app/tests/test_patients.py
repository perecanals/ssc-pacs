"""Tests for the patient-level listing sourced from the `patient` registry.

Regression coverage for the bug where patients with imaging but no
clinical row were invisible at the patient level, plus the
clinical-preferred / imaging-fallback stroke_date behavior, plus the
degradation when a registered clinical table does not exist.
"""

import contextlib

import psycopg2


def _find(items, patient_id, dataset=None):
    for it in items:
        if it["patient_id"] == patient_id and dataset in (None, it.get("dataset")):
            return it
    return None


@contextlib.contextmanager
def _table_hidden(dsn, table):
    """Rename `table` out of the way for the body, then restore it.

    Committed DDL on its own connection: the app under test reads through the
    pool, so the table must genuinely be gone as far as `to_regclass` is
    concerned. Restored even if the body raises, since the scratch DB is
    session-scoped and shared with every other test.
    """
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {table} RENAME TO {table}_hidden")
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {table}_hidden RENAME TO {table}")
        conn.close()


class TestPatientListing:
    def test_clinically_unmatched_patient_appears(self, logged_in_client):
        """P-0002 has imaging but no clinical row — must be listed."""
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
        row = _find(resp.json()["items"], "P-0001", "lvo")
        assert row is not None
        assert str(row["stroke_date"]).startswith("2025-01-01")

    def test_clinical_row_only_for_its_dataset(self, logged_in_client):
        """The clinical table is lvo's: only lvo's enrollment of P-0001 takes
        its date — the linked crisp2 enrollment keeps the imaging date."""
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"})
        dates = {r["patient_key"]: str(r["stroke_date"])[:10] for r in resp.json()["items"]}
        assert dates == {"lvo__P-0001": "2025-01-01", "crisp2__P-0001": "2025-02-02"}

    def test_datasets_endpoint_lists_registered_datasets(self, logged_in_client):
        """/api/datasets returns the registered dataset names, sorted."""
        resp = logged_in_client.get("/api/datasets")
        assert resp.status_code == 200
        assert resp.json() == ["crisp2", "lvo"]

    def test_dataset_filter_narrows_results(self, logged_in_client):
        """dataset=crisp2 isolates P-0001's crisp2 enrollment; P-0002 is excluded."""
        resp = logged_in_client.get("/api/patients", params={"dataset": "crisp2"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert _find(items, "P-0001") is not None
        assert _find(items, "P-0002") is None

    def test_one_row_per_enrollment(self, logged_in_client):
        """P-0001 is enrolled in two datasets: one patient row per enrollment,
        each with its own key and dataset, sharing one subject."""
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"})
        rows = {r["patient_key"]: r for r in resp.json()["items"]}
        assert set(rows) == {"lvo__P-0001", "crisp2__P-0001"}
        assert rows["lvo__P-0001"]["dataset"] == "lvo"
        assert rows["crisp2__P-0001"]["dataset"] == "crisp2"
        assert {r["subject_id"] for r in rows.values()} == {"lvo__P-0001"}

    def test_dataset_filter_shared_tag_keeps_both(self, logged_in_client):
        """dataset=lvo holds both patients' lvo enrollments → both listed."""
        resp = logged_in_client.get("/api/patients", params={"dataset": "lvo"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert _find(items, "P-0001") is not None
        assert _find(items, "P-0002") is not None

    def test_studies_and_series_expose_dataset_column(self, logged_in_client):
        """Study/series rows list their subject's enrollments' datasets, owner
        first (one row per study — never duplicated per enrollment)."""
        studies = logged_in_client.get("/api/studies").json()["items"]
        by_uid = {s["studyinstanceuid"]: s for s in studies}
        assert by_uid["1.2.3.4.5"]["dataset"] == "lvo, crisp2"
        assert by_uid["2.2.2.2.2"]["dataset"] == "lvo"

        series = logged_in_client.get("/api/series").json()["series"]
        assert any(s["dataset"] == "lvo, crisp2" for s in series)

        sub_rows = logged_in_client.get("/api/patients/lvo__P-0001/studies").json()
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
        assert series and all(
            set(s["patient_id"].split(", ")) == {"P-0001"} for s in series
        )

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


class TestWithoutClinicalTable:
    """A registered clinical table may go missing (dropped by hand).

    Without it the patient tab must still work, degrading to the imaging-derived
    stroke_date for everyone — the same value a clinically-unmatched patient
    already gets — rather than 500ing on a missing relation.
    """

    def test_patients_listed_without_clinical_table(self, logged_in_client, seeded_db):
        with _table_hidden(seeded_db, "lvo_clinical_data"):
            resp = logged_in_client.get("/api/patients")
            assert resp.status_code == 200
            items = resp.json()["items"]
            assert _find(items, "P-0001") is not None
            assert _find(items, "P-0002") is not None

    def test_stroke_date_falls_back_to_imaging(self, logged_in_client, seeded_db):
        """P-0001 prefers its clinical date (2025-01-01) only while the table
        exists; without it, its imaging date (2025-02-02) shows instead."""
        with _table_hidden(seeded_db, "lvo_clinical_data"):
            resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"})
            row = _find(resp.json()["items"], "P-0001", "lvo")
            assert str(row["stroke_date"]).startswith("2025-02-02")

    def test_stroke_date_filter_and_sort_still_work(self, logged_in_client, seeded_db):
        """Filter and sort reuse the same expression as the SELECT, so they must
        follow it into the no-clinical-table branch."""
        with _table_hidden(seeded_db, "lvo_clinical_data"):
            resp = logged_in_client.get(
                "/api/patients", params={"stroke_date": "2025-02-02"}
            )
            assert resp.status_code == 200
            assert _find(resp.json()["items"], "P-0001") is not None

            resp = logged_in_client.get(
                "/api/patients", params={"sort_by": "stroke_date", "sort_dir": "asc"}
            )
            assert resp.status_code == 200
            dates = [
                str(it["stroke_date"]) for it in resp.json()["items"]
                if it["stroke_date"] is not None
            ]
            assert dates == sorted(dates)

    def test_clinical_preference_restored_afterwards(self, logged_in_client):
        """Guard against the hide/restore helper leaking into other tests."""
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"})
        row = _find(resp.json()["items"], "P-0001", "lvo")
        assert str(row["stroke_date"]).startswith("2025-01-01")
