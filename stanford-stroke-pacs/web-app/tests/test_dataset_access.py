"""Unit + integration tests for dataset_access scopes and the DICOMweb guard."""

import dataset_access
import pytest
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
        assert dataset_access.fetch_study_datasets("0.0.0.unknown") is None

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
