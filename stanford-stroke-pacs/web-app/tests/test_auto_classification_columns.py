"""Machine-derived series_type / timepoint exposure on the browsing endpoints.

These are the "Auto Series Type" / "Auto Timepoint" columns: read-only, and a
separate axis from the human annotation labels of the same name.

The sort cases are the load-bearing ones. /api/series wraps a DISTINCT ON
subquery and orders by `sub.<col>`, so a column in SERIES_SORT_WHITELIST that is
missing from the inner SELECT raises UndefinedColumn at request time — nothing
else catches that.
"""

from tests.conftest import USER_CRISP, login_as

SERIES_AUTO_FIELDS = (
    "series_type",
    "series_type_rank",
    "series_label",
    "series_type_rule",
    "series_type_version",
)
STUDY_AUTO_FIELDS = (
    "timepoint",
    "timepoint_anchor_source",
    "hours_to_event",
    "timepoint_version",
)


def _find(rows, key, value):
    for r in rows:
        if r[key] == value:
            return r
    return None


class TestSeriesEndpoint:
    def test_exposes_series_and_study_auto_fields(self, logged_in_client):
        resp = logged_in_client.get("/api/series", params={"patient_id": "P-0001"})
        assert resp.status_code == 200
        row = _find(resp.json()["series"], "seriesinstanceuid", "1.2.3.4.5.6")
        assert row is not None
        assert row["series_type"] == "NCCT"
        assert row["series_type_rank"] == 1
        assert row["series_label"] == "NCCT_1"
        assert row["series_type_rule"] == "kernel-soft"
        assert row["series_type_version"] == "rules-v1"
        # Inherited from the owning study via the LEFT JOIN.
        assert row["timepoint"] == "BL"
        assert row["timepoint_anchor_source"] == "femoral_sheath_time"
        assert row["hours_to_event"] == -3.5

    def test_sort_by_auto_columns_does_not_500(self, logged_in_client):
        for col in ("series_type", "timepoint"):
            resp = logged_in_client.get("/api/series", params={"sort_by": col})
            assert resp.status_code == 200, f"sort_by={col} failed: {resp.text}"

    def test_filter_series_type(self, logged_in_client):
        hit = logged_in_client.get("/api/series", params={"series_type": "NCCT"})
        assert hit.status_code == 200
        assert _find(hit.json()["series"], "seriesinstanceuid", "1.2.3.4.5.6")

        miss = logged_in_client.get("/api/series", params={"series_type": "ZZZ"})
        assert miss.json()["series"] == []

    def test_filter_series_type_is_case_insensitive_substring(self, logged_in_client):
        resp = logged_in_client.get("/api/series", params={"series_type": "nc"})
        assert _find(resp.json()["series"], "seriesinstanceuid", "1.2.3.4.5.6")

    def test_filter_by_label_isolates_the_preferred_series(self, logged_in_client):
        """The point of the rank: NCCT_1 is the NCCT to use for that patient."""
        hit = logged_in_client.get("/api/series", params={"series_type": "NCCT_1"})
        assert _find(hit.json()["series"], "seriesinstanceuid", "1.2.3.4.5.6")

        miss = logged_in_client.get("/api/series", params={"series_type": "NCCT_2"})
        assert miss.json()["series"] == []

    def test_filter_by_study_timepoint(self, logged_in_client):
        hit = logged_in_client.get("/api/series", params={"timepoint": "bl"})
        assert _find(hit.json()["series"], "seriesinstanceuid", "1.2.3.4.5.6")

        miss = logged_in_client.get("/api/series", params={"timepoint": "FU"})
        assert _find(miss.json()["series"], "seriesinstanceuid", "1.2.3.4.5.6") is None


class TestStudyEndpoints:
    def test_list_studies_exposes_timepoint(self, logged_in_client):
        resp = logged_in_client.get("/api/studies", params={"patient_id": "P-0001"})
        assert resp.status_code == 200
        row = _find(resp.json()["items"], "studyinstanceuid", "1.2.3.4.5")
        for field in STUDY_AUTO_FIELDS:
            assert field in row
        assert row["timepoint"] == "BL"
        # series_type is series-level; it must not leak into the study row.
        assert "series_type" not in row

    def test_estimated_anchor_is_surfaced(self, logged_in_client):
        """P-0002's timepoint comes from an offset, not a recorded puncture time."""
        resp = logged_in_client.get("/api/studies", params={"patient_id": "P-0002"})
        row = _find(resp.json()["items"], "studyinstanceuid", "2.2.2.2.2")
        assert row["timepoint"] == "FU"
        assert row["timepoint_anchor_source"] == "time_recognized"

    def test_filter_studies_by_timepoint(self, logged_in_client):
        resp = logged_in_client.get("/api/studies", params={"timepoint": "BL"})
        items = resp.json()["items"]
        assert _find(items, "studyinstanceuid", "1.2.3.4.5")
        assert _find(items, "studyinstanceuid", "2.2.2.2.2") is None

    def test_sort_by_timepoint(self, logged_in_client):
        resp = logged_in_client.get("/api/studies", params={"sort_by": "timepoint"})
        assert resp.status_code == 200

    def test_unknown_sort_falls_back(self, logged_in_client):
        """series_type is not a column of image_study — must degrade, not 500."""
        resp = logged_in_client.get("/api/studies", params={"sort_by": "series_type"})
        assert resp.status_code == 200

    def test_patient_studies_expansion_exposes_timepoint(self, logged_in_client):
        resp = logged_in_client.get("/api/patients/P-0001/studies")
        assert resp.status_code == 200
        row = _find(resp.json(), "studyinstanceuid", "1.2.3.4.5")
        assert row["timepoint"] == "BL"
        assert row["timepoint_version"] == "rules-v1"

    def test_study_series_expansion_exposes_both(self, logged_in_client):
        """The sub-row endpoint had no image_study join until this feature."""
        resp = logged_in_client.get("/api/studies/1.2.3.4.5/series")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1, "the LEFT JOIN must not fan out series rows"
        row = rows[0]
        for field in SERIES_AUTO_FIELDS + STUDY_AUTO_FIELDS:
            assert field in row, f"{field} missing from /api/studies/{{uid}}/series"
        assert row["series_type"] == "NCCT"
        assert row["timepoint"] == "BL"


class TestDatasetScoping:
    def test_auto_filters_still_respect_dataset_scope(self, client):
        """user_crisp sees only P-0001; the new filter must not widen that."""
        login_as(client, USER_CRISP)
        resp = client.get("/api/series", params={"series_type": "NCCT"})
        assert resp.status_code == 200
        for row in resp.json()["series"]:
            assert row["patient_id"] == "P-0001"
