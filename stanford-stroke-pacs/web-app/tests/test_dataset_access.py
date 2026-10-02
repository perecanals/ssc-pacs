"""Unit + integration tests for dataset_access scopes and the DICOMweb guard."""

import asyncio
import json

import httpx
import pytest

import dataset_access
from routes import proxy
from tests.conftest import TEST_USER, USER_CRISP, USER_NONE, login_as

P1_STUDY = "1.2.3.4.5"
P2_STUDY = "2.2.2.2.2"


@pytest.fixture(autouse=True)
def _fresh_caches():
    """The module-level TTL caches outlive a TestClient — isolate tests."""
    dataset_access.clear_caches()
    yield
    dataset_access.clear_caches()


# ---------------------------------------------------------------------------
# scope_allows
# ---------------------------------------------------------------------------

class TestScopeAllows:
    def test_admin_scope_allows_everything(self):
        assert dataset_access.scope_allows(None, frozenset({"lvo"}))
        assert dataset_access.scope_allows(None, frozenset())
        assert dataset_access.scope_allows(None, None)

    def test_overlap_required(self):
        scope = frozenset({"crisp2"})
        assert dataset_access.scope_allows(scope, frozenset({"crisp2", "lvo"}))
        assert not dataset_access.scope_allows(scope, frozenset({"lvo"}))

    def test_unknown_or_untagged_entity_denied(self):
        scope = frozenset({"crisp2"})
        assert not dataset_access.scope_allows(scope, None)
        assert not dataset_access.scope_allows(scope, frozenset())

    def test_empty_scope_denies_all(self):
        assert not dataset_access.scope_allows(frozenset(), frozenset({"lvo"}))


# ---------------------------------------------------------------------------
# DB-backed scope resolution + caches
# ---------------------------------------------------------------------------

class TestScopeFetch:
    def test_admin_is_none(self, logged_in_client):
        assert dataset_access.fetch_user_scope(TEST_USER) is None

    def test_scoped_user(self, logged_in_client):
        assert dataset_access.fetch_user_scope(USER_CRISP) == frozenset({"crisp2"})

    def test_missing_user_denied(self, logged_in_client):
        assert dataset_access.fetch_user_scope("ghost") == frozenset()

    def test_study_datasets(self, logged_in_client):
        assert dataset_access.fetch_study_datasets(P2_STUDY) == frozenset({"lvo"})
        # P-0001's study belongs to its subject: both enrollments' datasets.
        assert dataset_access.fetch_study_datasets(P1_STUDY) == frozenset({"lvo", "crisp2"})
        assert dataset_access.fetch_study_datasets("0.0.0.unknown") is None

    def test_patient_id_subjects(self, logged_in_client):
        assert dataset_access.fetch_patient_id_subjects("P-0001") == (
            (frozenset({"lvo", "crisp2"}), frozenset({P1_STUDY})),
        )
        assert dataset_access.fetch_patient_id_subjects("P-ghost") is None

    def test_patient_id_subjects_cached(self, logged_in_client, monkeypatch):
        expected = ((frozenset({"lvo"}), frozenset({P2_STUDY})),)
        assert dataset_access.get_patient_id_subjects_cached("P-0002") == expected
        monkeypatch.setattr(
            dataset_access, "fetch_patient_id_subjects",
            lambda p: (_ for _ in ()).throw(AssertionError("DB hit on cached read")),
        )
        assert dataset_access.get_patient_id_subjects_cached("P-0002") == expected

    def test_visible_patient_id_studies(self):
        subjects = (
            (frozenset({"lvo"}), frozenset({"a"})),
            (frozenset({"crisp2"}), frozenset({"b"})),
        )
        visible = dataset_access.visible_patient_id_studies
        assert visible(None, subjects) == (frozenset({"a", "b"}), True)
        assert visible(frozenset({"crisp2"}), subjects) == (frozenset({"b"}), False)
        assert visible(frozenset({"x"}), subjects) == (frozenset(), False)
        assert visible(frozenset({"lvo"}), None) == (frozenset(), False)

    def test_cached_lookups_and_invalidation(self, logged_in_client, monkeypatch):
        assert dataset_access.get_user_scope_cached(USER_CRISP) == frozenset({"crisp2"})
        # Second call must come from the cache: poison the fetch to prove it.
        monkeypatch.setattr(
            dataset_access, "fetch_user_scope",
            lambda u: (_ for _ in ()).throw(AssertionError("DB hit on cached read")),
        )
        assert dataset_access.get_user_scope_cached(USER_CRISP) == frozenset({"crisp2"})
        # Invalidation forces a refetch.
        monkeypatch.setattr(
            dataset_access, "fetch_user_scope", lambda u: frozenset({"lvo"})
        )
        dataset_access.invalidate_user_scope(USER_CRISP)
        assert dataset_access.get_user_scope_cached(USER_CRISP) == frozenset({"lvo"})

    def test_admin_scope_cached_as_none(self, logged_in_client):
        assert dataset_access.get_user_scope_cached(TEST_USER) is None
        assert dataset_access.get_user_scope_cached(TEST_USER) is None  # cache hit


# ---------------------------------------------------------------------------
# DICOMweb proxy guard (deny paths — no upstream Orthanc needed)
# ---------------------------------------------------------------------------

class TestDicomwebGuard:
    def test_unauthenticated_401(self, client):
        assert client.get(f"/dicom-web/studies/{P1_STUDY}/metadata").status_code == 401

    def test_out_of_scope_study_403(self, client):
        login_as(client, USER_CRISP)
        resp = client.get(f"/dicom-web/studies/{P2_STUDY}/metadata")
        assert resp.status_code == 403

    def test_qido_query_param_out_of_scope_403(self, client):
        login_as(client, USER_CRISP)
        resp = client.get(f"/dicom-web/studies?StudyInstanceUID={P2_STUDY}")
        assert resp.status_code == 403

    def test_unscoped_qido_denied_for_non_admin(self, client):
        login_as(client, USER_CRISP)
        assert client.get("/dicom-web/studies").status_code == 403

    def test_unknown_study_denied(self, client):
        login_as(client, USER_CRISP)
        assert client.get("/dicom-web/studies/0.0.0.unknown/metadata").status_code == 403

    def test_no_grants_denied_even_in_no_uid_paths(self, client):
        login_as(client, USER_NONE)
        assert client.get("/dicom-web").status_code == 403

    def test_patientid_search_out_of_scope_403(self, client):
        # P-0002 is {lvo} only — crisp2's grant does not overlap.
        login_as(client, USER_CRISP)
        assert client.get("/dicom-web/studies?00100020=P-0002").status_code == 403

    def test_patientid_search_unknown_patient_403(self, client):
        login_as(client, USER_CRISP)
        assert client.get("/dicom-web/studies?00100020=P-ghost").status_code == 403

    def test_patientid_search_no_grants_403(self, client):
        login_as(client, USER_NONE)
        assert client.get("/dicom-web/studies?00100020=P-0001").status_code == 403


# ---------------------------------------------------------------------------
# DICOMweb proxy allow paths + query sanitization (stubbed upstream Orthanc)
# ---------------------------------------------------------------------------

@pytest.fixture()
def upstream(client, monkeypatch):
    """Replace the proxy's upstream client with a recorder after app startup.

    Depends on `client` so the app lifespan's init_client() has already run —
    otherwise it would overwrite the stub. Yields the list of proxied requests.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)

        async def _body():
            yield b"[]"

        # An async-generator body keeps the stream unconsumed for aiter_raw().
        return httpx.Response(
            200, content=_body(), headers={"content-type": "application/dicom+json"}
        )

    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", stub)
    yield captured
    asyncio.run(stub.aclose())


class TestPatientIdScopedSearch:
    OHIF_PANEL_QUERY = (
        "/dicom-web/studies?00100020={pid}&limit=101&offset=0"
        "&fuzzymatching=false&includefield=00081030%2C00080060"
    )

    def test_granted_patient_allowed(self, upstream, client):
        login_as(client, USER_CRISP)
        resp = client.get(self.OHIF_PANEL_QUERY.format(pid="P-0001"))
        assert resp.status_code == 200
        assert len(upstream) == 1

    def test_patientid_keyword_alias_allowed(self, upstream, client):
        login_as(client, USER_CRISP)
        resp = client.get("/dicom-web/studies?PatientID=P-0001")
        assert resp.status_code == 200

    def test_admin_allowed(self, upstream, client):
        login_as(client, TEST_USER)
        resp = client.get(self.OHIF_PANEL_QUERY.format(pid="P-0002"))
        assert resp.status_code == 200

    def test_modality_includefield_stripped_from_upstream(self, upstream, client):
        # The storage-forcing Modality (0008,0060) token must not reach
        # Orthanc; the rest of the query must survive intact.
        login_as(client, TEST_USER)
        client.get(self.OHIF_PANEL_QUERY.format(pid="P-0001"))
        query = upstream[0].url.params
        assert query["includefield"] == "00081030"
        assert query["00100020"] == "P-0001"
        assert query["limit"] == "101"

    def test_series_level_modality_untouched(self, upstream, client):
        # /studies/{uid}/series is series-level: Modality is index-answerable
        # there and must pass through unchanged.
        login_as(client, USER_CRISP)
        resp = client.get(f"/dicom-web/studies/{P1_STUDY}/series?includefield=00080060")
        assert resp.status_code == 200
        assert upstream[0].url.params["includefield"] == "00080060"


class TestSanitizeStudySearchQuery:
    def test_strips_modality_from_comma_list(self):
        q = proxy.sanitize_study_search_query(
            "00100020=P-0001&includefield=00081030%2C00080060&limit=101"
        )
        assert q == "00100020=P-0001&includefield=00081030&limit=101"

    def test_strips_keyword_form(self):
        q = proxy.sanitize_study_search_query("includefield=StudyDescription,Modality")
        assert q == "includefield=StudyDescription"

    def test_drops_param_when_empty(self):
        q = proxy.sanitize_study_search_query("00100020=P-0001&includefield=00080060")
        assert q == "00100020=P-0001"

    def test_repeated_includefield_params(self):
        q = proxy.sanitize_study_search_query(
            "includefield=00081030&includefield=00080060"
        )
        assert q == "includefield=00081030"

    def test_no_includefield_passthrough(self):
        q = "0020000D=1.2.3.4.5&limit=1"
        assert proxy.sanitize_study_search_query(q) == q


# ---------------------------------------------------------------------------
# Same PatientID, different people (separate enrollments AND subjects)
# ---------------------------------------------------------------------------

TWIN_STUDY = "7.7.7.7.7"


@pytest.fixture()
def same_id_other_person(db_conn):
    """A crisp2 patient that shares P-0002's id but is a different person.

    Orthanc groups both under one DICOM PatientID, so a PatientID search must
    not hand either dataset's user the other's studies.
    """
    from tests.conftest import insert_patient

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


@pytest.fixture()
def upstream_two_studies(client, monkeypatch):
    """Upstream Orthanc answering a PatientID search with both people's studies."""
    body = json.dumps([
        {"0020000D": {"vr": "UI", "Value": [uid]}} for uid in (P2_STUDY, TWIN_STUDY)
    ]).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/dicom+json"})

    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", stub)
    yield
    asyncio.run(stub.aclose())


class TestSharedPatientId:
    @staticmethod
    def _uids(resp):
        return {s["0020000D"]["Value"][0] for s in resp.json()}

    def test_study_search_filtered_to_visible_person(
        self, same_id_other_person, upstream_two_studies, client
    ):
        login_as(client, USER_CRISP)
        resp = client.get("/dicom-web/studies?00100020=P-0002")
        assert resp.status_code == 200
        assert self._uids(resp) == {TWIN_STUDY}

    def test_admin_sees_both(self, same_id_other_person, upstream_two_studies, client):
        login_as(client, TEST_USER)
        resp = client.get("/dicom-web/studies?00100020=P-0002")
        assert self._uids(resp) == {P2_STUDY, TWIN_STUDY}

    def test_non_study_search_denied_when_ambiguous(
        self, same_id_other_person, upstream_two_studies, client
    ):
        login_as(client, USER_CRISP)
        assert client.get("/dicom-web/series?00100020=P-0002").status_code == 403

    def test_filter_fails_closed_on_unexpected_body(self):
        assert proxy.filter_qido_studies(b"not json", frozenset({"x"})) == b"[]"
        assert proxy.filter_qido_studies(b'{"a": 1}', frozenset({"x"})) == b"[]"
