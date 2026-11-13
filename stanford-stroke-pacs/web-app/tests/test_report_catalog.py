"""Report discovery and STOW success handling."""
import json
from unittest.mock import MagicMock

import pytest

import report_catalog as catalog


def test_stow_only_uses_successful_sops(monkeypatch):
    calls = []

    def orthanc(method, path, **kw):
        calls.append((path, kw))
        return {
            "/tools/lookup": [{"Type": "Instance", "ID": "instance"}],
            "/instances/instance": {"ParentSeries": "series"},
            "/series/series": {"ParentStudy": "study"},
            "/studies/study": {"MainDicomTags": {"StudyInstanceUID": "1.2"}},
        }[path]

    monkeypatch.setattr(catalog, "orthanc_json", orthanc)
    sync = MagicMock()
    monkeypatch.setattr(catalog, "catalog_reports", sync)
    catalog.catalog_stow_response(json.dumps({
        "00081199": {"Value": [{"00081155": {"Value": ["1.2.3"]}}]},
        "00081198": {"Value": [{"00081155": {"Value": ["1.2.4"]}}]},
    }))
    assert calls[0] == ("/tools/lookup", {"data": "1.2.3"})
    sync.assert_called_once_with("1.2", execute=True)


@pytest.mark.parametrize("owner,existing,status", [
    (None, None, "skipped"),
    (("p", "d__p", "d__p"), ("filesystem", "1.2"), "skipped"),
    (("p", "d__p", "d__p"), ("orthanc", "other"), "skipped"),
    (("p", "d__p", "d__p"), None, "would catalogue"),
])
def test_discovery_never_overwrites_images_or_creates_studies(monkeypatch, owner, existing, status):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [owner, existing]
    monkeypatch.setattr(catalog, "get_conn", lambda: conn)
    monkeypatch.setattr(catalog, "orthanc_json", lambda method, path, **kw:
                        [{"MainDicomTags": {"SeriesInstanceUID": "1.2.3"}, "ParentStudy": "s"}]
                        if path == "/tools/find" else {"MainDicomTags": {"StudyInstanceUID": "1.2"}})
    assert catalog.catalog_reports() == [{"series": "1.2.3", "status": status}]
    assert not conn.commit.called
    assert all("INSERT" not in call.args[0] for call in cur.execute.call_args_list)


def test_catalog_failure_does_not_fail_successful_upload(monkeypatch, caplog):
    monkeypatch.setattr(catalog, "orthanc_json", MagicMock(side_effect=RuntimeError("offline")))
    catalog.catalog_stow_response('{"00081199":{"Value":[{"00081155":{"Value":["1.2"]}}]}}')
    assert "sync_ohif_reports.py --execute" in caplog.text


def test_catalog_roundtrip_and_delete(db_conn, monkeypatch, logged_in_client):
    import deletion
    from tests.conftest import insert_patient

    with db_conn.cursor() as cur:
        insert_patient(cur, "SR-CATALOG", "crisp2")
        cur.execute("INSERT INTO image_study (patient_id, studyinstanceuid) VALUES ('SR-CATALOG', '9.29.1')")
    db_conn.commit()
    proxy = MagicMock(wraps=db_conn)
    proxy.close = MagicMock()
    monkeypatch.setattr(catalog, "get_conn", lambda: proxy)
    report = {"ID": "sr", "ParentStudy": "st", "Instances": ["i"],
              "MainDicomTags": {"SeriesInstanceUID": "9.29.1.1"}}

    def orthanc(method, path, **kw):
        return {"/tools/find": [report], "/studies/st": {"MainDicomTags": {"StudyInstanceUID": "9.29.1"}},
                "/tools/lookup": [{"Type": "Series", "ID": "sr"}], "/series/sr": report}[path]

    monkeypatch.setattr(catalog, "orthanc_json", orthanc)
    for _ in range(2):
        catalog.catalog_reports(execute=True)
    with db_conn.cursor() as cur:
        cur.execute("SELECT storage_source, modality, dicom_dir_path, patient_key FROM image_series WHERE seriesinstanceuid='9.29.1.1'")
        assert cur.fetchone() == ("orthanc", "SR", None, "crisp2__SR-CATALOG")
        cur.execute("SELECT modalities FROM image_study WHERE studyinstanceuid='9.29.1'")
        assert cur.fetchone()[0] == ["SR"]
    for endpoint in ("/api/series?seriesinstanceuid=9.29.1.1", "/api/studies/9.29.1/series"):
        response = logged_in_client.get(endpoint)
        assert response.status_code == 200
        rows = response.json()
        if isinstance(rows, dict):
            rows = rows["series"]
        assert rows[0]["storage_source"] == "orthanc"
    assert logged_in_client.post("/api/series/9.29.1.1/warm").status_code == 409
    assert logged_in_client.post("/api/series/9.29.1.1/evict").status_code == 409
    assert logged_in_client.get("/api/series/9.29.1.1/nifti").status_code == 409
    plan = deletion.build_series_deletion_plan(db_conn, "9.29.1.1")
    assert plan["remove_dirs"] == plan["indexer_dirs"] == []
    monkeypatch.setattr(deletion, "delete_orthanc_series", lambda oid: False)
    with pytest.raises(RuntimeError):
        deletion.delete_index_and_db(db_conn, plan, execute=True)
    monkeypatch.setattr(deletion, "delete_orthanc_series", lambda oid: True)
    deletion.delete_index_and_db(db_conn, plan, execute=True)
    with db_conn.cursor() as cur:
        cur.execute("SELECT modalities FROM image_study WHERE studyinstanceuid='9.29.1'")
        assert cur.fetchone()[0] is None
        cur.execute("DELETE FROM image_study_labelled WHERE studyinstanceuid='9.29.1'")
        cur.execute("DELETE FROM image_study WHERE studyinstanceuid='9.29.1'")
        cur.execute("DELETE FROM patient WHERE patient_id='SR-CATALOG'")
    db_conn.commit()


def test_xml_stow_only_returns_successful_instances():
    body = b"""<NativeDicomModel xmlns="http://dicom.nema.org/PS3.19/models/NativeDICOM">
      <DicomAttribute tag="00081199"><Item number="1">
        <DicomAttribute tag="00081155"><Value number="1">1.2.3</Value></DicomAttribute>
      </Item></DicomAttribute>
      <DicomAttribute tag="00081198"><Item number="1">
        <DicomAttribute tag="00081155"><Value number="1">1.2.4</Value></DicomAttribute>
      </Item></DicomAttribute>
    </NativeDicomModel>"""
    assert catalog.successful_sops(body) == ["1.2.3"]
