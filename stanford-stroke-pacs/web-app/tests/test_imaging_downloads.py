"""Dataset authorization, Unicode HTTP headers, conversion fidelity and cleanup."""

import io
import subprocess
import threading
from zipfile import ZipFile

import numpy as np
import pydicom
import pytest
import SimpleITK as sitk
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

import imaging_downloads as downloads
from data_exports import database
from tests.conftest import USER_CRISP, USER_LVO, USER_NONE, login_as

UID = "1.2.3.4.5.6"


@pytest.fixture()
def imaging(client, tmp_path, monkeypatch):
    source = tmp_path / "CTA^é³测试 'scan'"
    source.mkdir()
    for z in range(3):
        meta = FileMetaDataset()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        ds = FileDataset(str(source / f"slice^{z}³.dcm"), {}, file_meta=meta, preamble=b"\0" * 128)
        ds.SOPClassUID = meta.MediaStorageSOPClassUID
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.SeriesInstanceUID = UID
        ds.StudyInstanceUID = "1.2.3.4.5"
        ds.PatientID = "synthetic"
        ds.Modality = "CT"
        ds.Rows = 4
        ds.Columns = 5
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.ImagePositionPatient = [0, 0, z * 2]
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.PixelSpacing = [0.75, 0.5]
        ds.SliceThickness = 2
        ds.InstanceNumber = z + 1
        ds.PixelData = np.full((4, 5), z + 10, dtype=np.int16).tobytes()
        ds.save_as(ds.filename, enforce_file_format=True)
    before = database.records(
        "SELECT dicom_dir_path, dicom_archive_path, seriesdescription FROM image_series WHERE seriesinstanceuid=%s",
        (UID,),
        one=True,
    )
    database.records(
        "UPDATE image_series SET dicom_dir_path=%s, dicom_archive_path=NULL, seriesdescription=%s WHERE seriesinstanceuid=%s",
        (str(source), 'CTA^é³测试 "quoted"\r\nname', UID),
    )
    database.records("UPDATE users SET is_staff=true WHERE username=ANY(%s)", ([USER_CRISP, USER_NONE],))
    spool = tmp_path / "temporary"
    spool.mkdir()
    original_mkdtemp = downloads.tempfile.mkdtemp
    monkeypatch.setattr(downloads.tempfile, "mkdtemp", lambda **kwargs: original_mkdtemp(dir=spool, **kwargs))
    monkeypatch.setattr(downloads, "_slots", threading.BoundedSemaphore(2))
    try:
        yield client, source, spool
    finally:
        database.records(
            "UPDATE image_series SET dicom_dir_path=%s, dicom_archive_path=%s, seriesdescription=%s WHERE seriesinstanceuid=%s",
            (before["dicom_dir_path"], before["dicom_archive_path"], before["seriesdescription"], UID),
        )
        database.records("UPDATE users SET is_staff=false WHERE username=ANY(%s)", ([USER_CRISP, USER_NONE],))


@pytest.mark.parametrize("format", ["dicom-zip", "nifti"])
def test_download_permission_boundary(imaging, monkeypatch, format):
    client, _, _ = imaging
    url = f"/api/series/{UID}/{format}"

    def no_files(*args):
        pytest.fail("Unauthorized request accessed files")

    monkeypatch.setattr(downloads, "source_directory", no_files)
    assert client.get(url).status_code == 401
    login_as(client, USER_LVO)  # Regular user, despite having the right dataset.
    assert client.get(url).status_code == 403
    login_as(client, USER_NONE)  # Staff with no permitted datasets.
    assert client.get(url).status_code == 404
    login_as(client, USER_CRISP)
    assert client.get("/api/series/2.2.2.2.2.9/" + format).status_code == 404
    assert client.get(f"/api/series/{UID}/paths").status_code == 403


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("format", ["dicom-zip", "nifti"])
def test_unicode_download_and_real_nifti_geometry(imaging, tmp_path, cold, format):
    client, source, spool = imaging
    if cold:
        archive = tmp_path / "CTA^é³.tar.zst"
        subprocess.run(["tar", "--zstd", "-cf", str(archive), "-C", str(source), "."], check=True, capture_output=True)
        database.records(
            "UPDATE image_series SET dicom_archive_path=%s WHERE seriesinstanceuid=%s", (str(archive), UID)
        )
    login_as(client, USER_CRISP)
    response = client.get(f"/api/series/{UID}/{format}")
    assert response.status_code == 200, response.text[:200] if response.status_code != 200 else ""
    header = response.headers["content-disposition"]
    assert header.isascii() and "\r" not in header and "\n" not in header
    assert "filename*=UTF-8''" in header and "%E6%B5%8B%E8%AF%95" in header
    assert response.headers["cache-control"] == "no-store"
    assert not list(spool.iterdir())
    if format == "dicom-zip":
        with ZipFile(io.BytesIO(response.content)) as archive:
            files = [n for n in archive.namelist() if n.endswith(".dcm")]
            assert len(files) == 3
            assert all(pydicom.dcmread(io.BytesIO(archive.read(n))).SeriesInstanceUID == UID for n in files)
    else:
        output = tmp_path / "result.nii.gz"
        output.write_bytes(response.content)
        image = sitk.ReadImage(str(output))
        assert image.GetSize() == (5, 4, 3)
        assert image.GetSpacing() == pytest.approx((0.5, 0.75, 2))
        assert image.GetOrigin() == pytest.approx((0, 0, 0))
        assert image.GetDirection() == pytest.approx((1, 0, 0, 0, 1, 0, 0, 0, 1))
        np.testing.assert_array_equal(sitk.GetArrayFromImage(image)[:, 0, 0], [10, 11, 12])
    assert len(list(source.glob("*.dcm"))) == 3
    assert not list(source.glob("*.nii*"))


@pytest.mark.parametrize("timeout", [False, True])
def test_conversion_failure_cleans_up_and_releases_slot(imaging, monkeypatch, timeout):
    client, _, spool = imaging
    login_as(client, USER_CRISP)

    def fail(command, **kwargs):
        assert isinstance(command, list) and "shell" not in kwargs
        if timeout:
            raise subprocess.TimeoutExpired(command, 300)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(downloads.subprocess, "run", fail)
    for _ in range(3):
        assert client.get(f"/api/series/{UID}/nifti").status_code == (504 if timeout else 422)
        assert not list(spool.iterdir())


def test_download_concurrency_limit(imaging, monkeypatch):
    client, _, spool = imaging
    login_as(client, USER_CRISP)
    monkeypatch.setattr(downloads, "_slots", threading.BoundedSemaphore(0))
    assert client.get(f"/api/series/{UID}/nifti").status_code == 429
    assert not list(spool.iterdir())
