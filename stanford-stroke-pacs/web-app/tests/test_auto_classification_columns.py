"""Machine-derived series_type / timepoint exposure on the browsing endpoints.

These are the "Auto Series Type" / "Auto Timepoint" columns: read-only, and a
separate axis from the human annotation labels of the same name.

The sort cases are the load-bearing ones. /api/series wraps a DISTINCT ON
subquery and orders by `sub.<col>`, so a column in SERIES_SORT_WHITELIST that is
missing from the inner SELECT raises UndefinedColumn at request time — nothing
else catches that.
"""

import psycopg2
import pytest

from tests.conftest import USER_CRISP, USER_NONE, login_as

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


@pytest.fixture()
def two_series_study(seeded_db):
    """A P-0001 study carrying two differently-typed series (CTA + NCCT).

    The session-scoped seed has one series per study, so it can't exercise
    within-study narrowing; the shared exact-set assertions elsewhere forbid
    adding matching rows globally. This inserts + COMMITs a dedicated study so
    the API's own connection sees it, then removes it on teardown. Tests run
    sequentially, so no other test observes these rows.
    """
    uid = "9.9.9.9.9"
    conn = psycopg2.connect(**seeded_db)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO image_study "
                "(patient_id, studyinstanceuid, study_type, acquisitiondatetime, "
                " timepoint, timepoint_anchor_source, hours_to_event, timepoint_version) "
                "VALUES ('P-0001', %s, 'CTA', '2025-04-04', "
                " 'BL', 'femoral_sheath_time', -2.0, 'rules-v1')",
                (uid,),
            )
            cur.execute(
                "INSERT INTO image_series "
                "(patient_id, studyinstanceuid, seriesinstanceuid, modality, seriesdescription, "
                " series_type, series_type_rank, series_label, series_type_rule, series_type_version) "
                "VALUES "
                " ('P-0001', %s, '9.9.9.9.9.1', 'CT', 'Angio', 'CTA', 1, 'CTA_1', 'kernel-soft', 'rules-v1'), "
                " ('P-0001', %s, '9.9.9.9.9.2', 'CT', 'Axial', 'NCCT', 1, 'NCCT_1', 'kernel-soft', 'rules-v1')",
                (uid, uid),
            )
        yield uid
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM image_series WHERE studyinstanceuid = %s", (uid,))
            cur.execute("DELETE FROM image_study WHERE studyinstanceuid = %s", (uid,))
        conn.close()


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

    # The sidebar quick filters cascade into these expandable sub-row endpoints,
    # so an expanded subtable mirrors the top-level filter (same "has-one" at the
    # study level / direct match at the series level as the flat endpoints).
    def test_patient_studies_filter_by_timepoint(self, logged_in_client):
        hit = logged_in_client.get("/api/patients/P-0001/studies?timepoint=BL")
        assert _find(hit.json(), "studyinstanceuid", "1.2.3.4.5")
        miss = logged_in_client.get("/api/patients/P-0001/studies?timepoint=FU")
        assert miss.json() == []

    def test_patient_studies_filter_series_type_is_has_one(self, logged_in_client):
        # P-0001's only study has an NCCT series but no CTA series.
        hit = logged_in_client.get("/api/patients/P-0001/studies?series_type=NCCT")
        assert _find(hit.json(), "studyinstanceuid", "1.2.3.4.5")
        miss = logged_in_client.get("/api/patients/P-0001/studies?series_type=CTA")
        assert miss.json() == []

    def test_patient_studies_filters_and_together(self, logged_in_client):
        both = logged_in_client.get(
            "/api/patients/P-0001/studies?series_type=NCCT&timepoint=BL"
        )
        assert _find(both.json(), "studyinstanceuid", "1.2.3.4.5")
        neither = logged_in_client.get(
            "/api/patients/P-0001/studies?series_type=NCCT&timepoint=FU"
        )
        assert neither.json() == []

    def test_patient_studies_import_label_coexists_with_new_params(
        self, logged_in_client
    ):
        # A non-matching import label ANDs with (matching) timepoint -> empty,
        # proving the refactored import-label fragment still binds correctly
        # alongside the new params.
        resp = logged_in_client.get(
            "/api/patients/P-0001/studies?study_import_label=zzz&timepoint=BL"
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_study_series_filter_by_series_type(self, logged_in_client):
        hit = logged_in_client.get("/api/studies/1.2.3.4.5/series?series_type=NCCT")
        assert len(hit.json()) == 1
        miss = logged_in_client.get("/api/studies/1.2.3.4.5/series?series_type=CTA")
        assert miss.json() == []

    def test_study_series_filter_by_owning_study_timepoint(self, logged_in_client):
        hit = logged_in_client.get("/api/studies/1.2.3.4.5/series?timepoint=BL")
        assert len(hit.json()) == 1
        miss = logged_in_client.get("/api/studies/1.2.3.4.5/series?timepoint=FU")
        assert miss.json() == []

    def test_study_series_narrows_within_a_multi_series_study(
        self, logged_in_client, two_series_study
    ):
        """Study with a CTA and an NCCT series: filtering CTA leaves only the CTA."""
        uid = two_series_study
        allrows = logged_in_client.get(f"/api/studies/{uid}/series")
        assert len(allrows.json()) == 2
        cta = logged_in_client.get(f"/api/studies/{uid}/series?series_type=CTA")
        types = [r["series_type"] for r in cta.json()]
        assert types == ["CTA"]


class TestSidebarQuickFilters:
    """The sidebar multi-select: repeated params, ORed, at every level."""

    def test_classification_values_vocabulary(self, logged_in_client):
        resp = logged_in_client.get("/api/classification-values")
        assert resp.status_code == 200
        body = resp.json()
        assert {"value": "NCCT", "count": 1} in body["series_types"]
        tps = [t["value"] for t in body["timepoints"]]
        assert "BL" in tps and "FU" in tps
        # Clinical order (pre / during / post puncture), not alphabetical.
        assert tps.index("BL") < tps.index("FU")

    def test_repeated_values_are_ored(self, logged_in_client):
        # The NCCT series matches even though CTA does not.
        resp = logged_in_client.get("/api/series?series_type=NCCT&series_type=CTA")
        assert _find(resp.json()["series"], "seriesinstanceuid", "1.2.3.4.5.6")

        none = logged_in_client.get("/api/series?series_type=CTA&series_type=CTP")
        assert none.json()["series"] == []

    def test_patients_filtered_by_the_series_they_have(self, logged_in_client):
        hit = logged_in_client.get("/api/patients", params={"series_type": "NCCT_1"})
        ids = {r["patient_id"] for r in hit.json()["items"]}
        assert ids == {"P-0001"}, "only P-0001 has a classified series"

    def test_patients_filtered_by_the_timepoint_they_have(self, logged_in_client):
        bl = logged_in_client.get("/api/patients", params={"timepoint": "BL"})
        assert {r["patient_id"] for r in bl.json()["items"]} == {"P-0001"}

        fu = logged_in_client.get("/api/patients", params={"timepoint": "FU"})
        assert {r["patient_id"] for r in fu.json()["items"]} == {"P-0002"}

    def test_studies_filtered_by_the_series_they_contain(self, logged_in_client):
        hit = logged_in_client.get("/api/studies", params={"series_type": "NCCT"})
        uids = {r["studyinstanceuid"] for r in hit.json()["items"]}
        assert uids == {"1.2.3.4.5"}, "P-0002's study has no series"

    def test_auto_filters_combine_as_and(self, logged_in_client):
        both = logged_in_client.get(
            "/api/patients", params={"series_type": "NCCT", "timepoint": "BL"}
        )
        assert {r["patient_id"] for r in both.json()["items"]} == {"P-0001"}

        # P-0002 has the FU study but no NCCT series -> the AND excludes it.
        neither = logged_in_client.get(
            "/api/patients", params={"series_type": "NCCT", "timepoint": "FU"}
        )
        assert neither.json()["items"] == []


class TestDatasetScoping:
    def test_auto_filters_still_respect_dataset_scope(self, client):
        """user_crisp sees only P-0001; the new filter must not widen that."""
        login_as(client, USER_CRISP)
        resp = client.get("/api/series", params={"series_type": "NCCT"})
        assert resp.status_code == 200
        for row in resp.json()["series"]:
            assert row["patient_id"] == "P-0001"

    def test_subtable_filters_do_not_bypass_scope(self, client):
        """A user with no grants can't reach the sub-row endpoints, filter or not."""
        login_as(client, USER_NONE)
        studies = client.get("/api/patients/P-0001/studies?series_type=NCCT")
        assert studies.status_code == 404
        series = client.get("/api/studies/1.2.3.4.5/series?series_type=NCCT")
        assert series.status_code == 404
