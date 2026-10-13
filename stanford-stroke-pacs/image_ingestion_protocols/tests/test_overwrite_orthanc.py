"""Overwrite must discard stale registrations even when DICOM UIDs persist."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import execute_image_ingestion_protocol as executor
import image_ingestion_protocol as protocol


@pytest.fixture
def session(monkeypatch):
    fake = Mock()
    fake.__enter__ = Mock(return_value=fake)
    fake.__exit__ = Mock(return_value=False)
    fake.post.return_value.json.return_value = [
        {"Type": "Study", "ID": "old-study"},
        {"Type": "Series", "ID": "other-resource"},
    ]
    fake.delete.return_value.status_code = 200
    monkeypatch.setattr(protocol, "STORAGE_MODE", "cold_path_cache")
    monkeypatch.setattr(protocol.requests, "Session", lambda: fake)
    return fake


def test_overwrite_removes_only_matching_study(session):
    protocol.ImageIngestionProtocol._reset_orthanc_study_for_overwrite("1.2.study")
    assert session.post.call_args.kwargs["data"] == "1.2.study"
    assert session.delete.call_count == 1
    assert session.delete.call_args.args[0].endswith("/studies/old-study")


@pytest.mark.parametrize("stage", ["post", "delete"])
def test_overwrite_does_not_hide_orthanc_failure(session, stage):
    getattr(session, stage).return_value.raise_for_status.side_effect = requests.HTTPError("unavailable")
    with pytest.raises(requests.HTTPError):
        protocol.ImageIngestionProtocol._reset_orthanc_study_for_overwrite("1.2.study")
    if stage == "post":
        session.delete.assert_not_called()


def test_overwrite_absent_study_is_safe(session):
    session.post.return_value.json.return_value = []
    protocol.ImageIngestionProtocol._reset_orthanc_study_for_overwrite("1.2.study")
    session.delete.assert_not_called()


def test_overwrite_legacy_does_not_contact_orthanc(session, monkeypatch):
    monkeypatch.setattr(protocol, "STORAGE_MODE", "legacy")
    protocol.ImageIngestionProtocol._reset_orthanc_study_for_overwrite("1.2.study")
    session.post.assert_not_called()


def test_ingestion_forces_only_target_series_not_cold_siblings(monkeypatch):
    import scoped_index

    target = SimpleNamespace(n_instances=72)
    monkeypatch.setattr(executor, "STORAGE_MODE", "cold_path_cache")
    monkeypatch.setattr(executor, "_series_targets_from_db", lambda *a: [target])
    monkeypatch.setattr(executor, "_orthanc_session", lambda: (Mock(), "http://orthanc"))
    scan = Mock(return_value={"registered": 1, "targets": 1, "registered_suids": ["series"]})
    monkeypatch.setattr(scoped_index, "scoped_register_series", scan)
    assert executor.index_case_into_orthanc(None, Mock(), ["series"]) == ["series"]
    assert scan.call_args.kwargs["force"] is True
    assert scan.call_args.kwargs["granularity"] == "series"
