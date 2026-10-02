"""Patient identity (Alembic 0026): enrollments, subjects, imaging ownership.

Seeded geometry (conftest.py): P-0001 is enrolled in lvo (owns study 1.2.3.4.5)
and in crisp2 (linked: same subject); P-0002 in lvo only. The `twin` fixture
adds a *different person* who shares P-0002's id in crisp2 — the case the
identity model exists for.
"""

from __future__ import annotations

import pytest

from tests.conftest import USER_CRISP, USER_LVO, insert_patient, login_as

P1_STUDY = "1.2.3.4.5"
P1_SERIES = "1.2.3.4.5.6"
P2_STUDY = "2.2.2.2.2"
TWIN_STUDY = "8.8.8.8.8"


@pytest.fixture()
def labels(db_conn):
    """Patient labels on both of P-0001's enrollments (same label name)."""
    with db_conn.cursor() as cur:
        for key, value in (("lvo__P-0001", "from-lvo"), ("crisp2__P-0001", "from-crisp2")):
            cur.execute(
                "INSERT INTO annotations (level, patient_key, patient_id, label, value, created_by) "
                "VALUES ('patient', %s, 'P-0001', 'ident_flag', %s, 'pytest')",
                (key, value),
            )
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM annotations WHERE label LIKE 'ident_%'")
    db_conn.commit()


@pytest.fixture()
def twin(db_conn):
    with db_conn.cursor() as cur:
        key = insert_patient(cur, "P-0002", "crisp2")
        cur.execute(
            "INSERT INTO image_study (patient_id, patient_key, subject_id, studyinstanceuid) "
            "VALUES ('P-0002', %s, %s, %s)",
            (key, key, TWIN_STUDY),
        )
    db_conn.commit()
    yield key
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM image_study WHERE studyinstanceuid = %s", (TWIN_STUDY,))
        cur.execute("DELETE FROM patient WHERE patient_key = %s", (key,))
    db_conn.commit()


def _study(rows, uid=P1_STUDY):
    return next(r for r in rows if r["studyinstanceuid"] == uid)


def _inherited(row, label="ident_flag"):
    return sorted(
        (a["patient_key"], a["value"]) for a in row["inherited_annotations"] if a["label"] == label
    )


class TestStudyRowIdentity:
    def test_admin_sees_every_enrollment_owner_first(self, logged_in_client):
        row = _study(logged_in_client.get("/api/studies").json()["items"])
        assert row["patient_id"] == "P-0001, P-0001"
        assert row["dataset"] == "lvo, crisp2"
        assert row["patient_key"] == "lvo__P-0001"  # the owner

    def test_scoped_user_sees_only_own_dataset_identifiers(self, client):
        row = _study(login_as(client, USER_CRISP).get("/api/studies").json()["items"])
        assert (row["patient_id"], row["dataset"]) == ("P-0001", "crisp2")

    def test_patient_id_filter_matches_any_in_scope_enrollment(self, client):
        login_as(client, USER_CRISP)
        hit = client.get("/api/studies", params={"patient_id": "P-0001"}).json()["items"]
        assert [r["studyinstanceuid"] for r in hit] == [P1_STUDY]


@pytest.mark.usefixtures("labels")
class TestInheritedPatientLabels:
    def test_admin_inherits_from_both_enrollments_and_cannot_edit_ambiguously(
        self, logged_in_client
    ):
        row = _study(logged_in_client.get("/api/studies").json()["items"])
        assert _inherited(row) == [
            ("crisp2__P-0001", "from-crisp2"), ("lvo__P-0001", "from-lvo"),
        ]
        assert row["edit_patient_key"] is None

    def test_scoped_user_inherits_only_own_dataset_labels(self, client):
        row = _study(login_as(client, USER_CRISP).get("/api/studies").json()["items"])
        assert _inherited(row) == [("crisp2__P-0001", "from-crisp2")]
        assert row["edit_patient_key"] == "crisp2__P-0001"

    def test_series_rows_follow_the_same_rule(self, client):
        rows = login_as(client, USER_LVO).get(f"/api/studies/{P1_STUDY}/series").json()
        row = next(r for r in rows if r["seriesinstanceuid"] == P1_SERIES)
        assert _inherited(row) == [("lvo__P-0001", "from-lvo")]
        assert row["edit_patient_key"] == "lvo__P-0001"

    def test_expanded_patient_inherits_from_that_enrollment_only(self, logged_in_client):
        rows = logged_in_client.get("/api/patients/crisp2__P-0001/studies").json()
        row = _study(rows)
        assert _inherited(row) == [("crisp2__P-0001", "from-crisp2")]
        assert row["edit_patient_key"] == "crisp2__P-0001"

    def test_patient_label_filter_is_scoped(self, client):
        flt = '[{"label": "ident_flag", "level": "patient", "value": "from-lvo", "datatype": "text"}]'
        login_as(client, USER_LVO)
        assert client.get("/api/studies", params={"label_filters": flt}).json()["total"] == 1
        # The lvo label exists on this study's subject, but not in crisp2's view.
        login_as(client, USER_CRISP)
        assert client.get("/api/studies", params={"label_filters": flt}).json()["total"] == 0

    def test_patient_label_filter_from_patients_tab_is_per_enrollment(self, logged_in_client):
        flt = '[{"label": "ident_flag", "level": "patient", "value": "from-crisp2", "datatype": "text"}]'
        rows = logged_in_client.get("/api/patients", params={"label_filters": flt}).json()["items"]
        assert [r["patient_key"] for r in rows] == ["crisp2__P-0001"]


class TestAnnotationWrites:
    @pytest.fixture(autouse=True)
    def _cleanup(self, db_conn):
        yield
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM annotations WHERE label LIKE 'ident_%'")
        db_conn.commit()

    def test_study_annotation_stores_owner_patient_id(self, logged_in_client, db_conn):
        # Clients may send the joined display string; it is never trusted.
        resp = logged_in_client.post("/api/annotations", json={
            "level": "study", "studyinstanceuid": P1_STUDY,
            "patient_id": "P-0001, P-0001", "label": "ident_study", "value": "x",
        })
        assert resp.status_code == 201
        with db_conn.cursor() as cur:
            cur.execute("SELECT patient_id FROM annotations WHERE label = 'ident_study'")
            assert cur.fetchone()[0] == "P-0001"

    def test_patient_annotation_unknown_key_404(self, logged_in_client):
        resp = logged_in_client.post("/api/annotations", json={
            "level": "patient", "patient_key": "lvo__P-ghost", "label": "ident_x", "value": "x",
        })
        assert resp.status_code == 404

    def test_same_label_on_two_enrollments_is_two_values(self, logged_in_client):
        for key, value in (("lvo__P-0001", "a"), ("crisp2__P-0001", "b")):
            resp = logged_in_client.post("/api/annotations", json={
                "level": "patient", "patient_key": key, "label": "ident_two", "value": value,
            })
            assert resp.status_code == 201
        rows = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"}).json()["items"]
        values = {
            r["patient_key"]: [a["value"] for a in r["annotations"] if a["label"] == "ident_two"]
            for r in rows
        }
        assert values == {"lvo__P-0001": ["a"], "crisp2__P-0001": ["b"]}


@pytest.mark.usefixtures("twin")
class TestSameIdDifferentPerson:
    def test_two_people_two_patient_rows(self, logged_in_client):
        rows = logged_in_client.get("/api/patients", params={"patient_id": "P-0002"}).json()["items"]
        assert {r["patient_key"]: r["subject_id"] for r in rows} == {
            "lvo__P-0002": "lvo__P-0002", "crisp2__P-0002": "crisp2__P-0002",
        }

    def test_expanding_one_does_not_show_the_other(self, logged_in_client):
        lvo = logged_in_client.get("/api/patients/lvo__P-0002/studies").json()
        crisp = logged_in_client.get("/api/patients/crisp2__P-0002/studies").json()
        assert [r["studyinstanceuid"] for r in lvo] == [P2_STUDY]
        assert [r["studyinstanceuid"] for r in crisp] == [TWIN_STUDY]

    def test_scoped_users_see_only_their_person(self, client):
        login_as(client, USER_CRISP)
        uids = {r["studyinstanceuid"] for r in client.get("/api/studies").json()["items"]}
        assert TWIN_STUDY in uids and P2_STUDY not in uids
        login_as(client, USER_LVO)
        uids = {r["studyinstanceuid"] for r in client.get("/api/studies").json()["items"]}
        assert P2_STUDY in uids and TWIN_STUDY not in uids

    def test_study_access_follows_the_subject(self, client):
        login_as(client, USER_LVO)
        assert client.get(f"/api/studies/{TWIN_STUDY}/series").status_code == 404
        assert client.get(f"/api/studies/{P2_STUDY}/series").status_code == 200
