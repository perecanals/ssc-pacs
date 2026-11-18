"""OHIF SR/SEG upload authorization, wire compatibility and resource bounds."""

import asyncio
from io import BytesIO
from tempfile import SpooledTemporaryFile

import httpx
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from starlette.requests import ClientDisconnect, Request

import annotation_upload as uploads
import dataset_access
from routes import proxy
from tests.conftest import TEST_USER, USER_CRISP, USER_NONE, login_as

P1 = "1.2.3.4.5"  # crisp2 member of lvo-owned study
P2 = "2.2.2.2.2"  # lvo only
SR = "1.2.840.10008.5.1.4.1.1.88.22"
SEG = "1.2.840.10008.5.1.4.1.1.66.4"


def dicom(study=P1, sop_class=SR, references=(), **changes):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = sop_class
    ds.Modality = "SEG" if sop_class == SEG else "SR"
    if references:
        ds.CurrentRequestedProcedureEvidenceSequence = []
        for ref in references:
            item = Dataset()
            item.StudyInstanceUID = ref
            ds.CurrentRequestedProcedureEvidenceSequence.append(item)
    for key, val in changes.items():
        if val is None:
            del ds[key]
        else:
            setattr(ds, key, val)
    out = BytesIO()
    pydicom.dcmwrite(out, ds, enforce_file_format=True)
    return out.getvalue()


def multipart(*parts, boundary="ohif-boundary"):
    # Match dicomweb-client's leading CRLF and lack of trailing CRLF.
    return b"".join(
        b'\r\n--' + boundary.encode() + b'\r\nContent-Type: application/dicom\r\n\r\n' + part
        for part in parts
    ) + b'\r\n--' + boundary.encode() + b'--'


CONTENT_TYPE = 'multipart/related; type="application/dicom"; boundary="ohif-boundary"'


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    import sr_provenance
    monkeypatch.setattr(sr_provenance, "orthanc_json", lambda *a, **kw: [])
    dataset_access.clear_caches()
    yield
    dataset_access.clear_caches()


@pytest.fixture()
def upstream(client, monkeypatch):
    captured = []

    async def handler(request):
        body = await request.aread()
        captured.append((request, body))

        async def response():
            yield b'{"saved":true}'

        return httpx.Response(
            200, content=response(), headers={"content-type": "application/dicom+json"},
        )

    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", stub)
    yield captured
    asyncio.run(stub.aclose())


def post(client, body, url="/dicom-web/studies", content_type=CONTENT_TYPE):
    return client.post(url, content=body, headers={"Content-Type": content_type})


@pytest.mark.parametrize("sop_class", sorted(uploads.ANNOTATION_SOP_CLASSES))
@pytest.mark.parametrize("url", ["/dicom-web/studies", f"/dicom-web/studies/{P1}"])
def test_annotations_allowed_and_forwarded_verbatim(client, upstream, sop_class, url):
    login_as(client, USER_CRISP)
    body = multipart(dicom(sop_class=sop_class, references=[P1]))
    resp = post(client, body, url)
    assert resp.status_code == 200
    assert resp.json() == {"saved": True}
    request, forwarded = upstream[0]
    assert forwarded == body
    assert request.headers["content-type"] == CONTENT_TYPE
    assert int(request.headers["content-length"]) == len(body)
    assert "cookie" not in request.headers


@pytest.mark.parametrize("user,status", [(None, 401), (USER_NONE, 403)])
def test_missing_access_denied(client, upstream, user, status):
    if user:
        login_as(client, user)
    assert post(client, multipart(dicom())).status_code == status
    assert not upstream


@pytest.mark.parametrize("parts", [
    [dicom(P2)], [dicom("9.9.9")], [dicom(), dicom(P2)],
    [dicom(references=[P2])], [dicom(references=["9.9.9"])],
], ids=["outside", "unknown", "mixed-batch", "outside-reference", "unknown-reference"])
def test_all_parts_and_evidence_must_be_in_scope(client, upstream, parts):
    login_as(client, USER_CRISP)
    assert post(client, multipart(*parts)).status_code == 403
    assert not upstream


@pytest.mark.parametrize("url", [
    f"/dicom-web/studies/{P1}", f"/dicom-web/studies?StudyInstanceUID={P1}",
    f"/dicom-web/studies?0020000D={P1}",
])
def test_spoofed_url_cannot_authorize_payload(client, upstream, url):
    login_as(client, USER_CRISP)
    assert post(client, multipart(dicom(P2)), url).status_code == 400
    assert not upstream


@pytest.mark.parametrize("body", [
    b"", multipart(b"not dicom"), multipart(dicom())[:-10], multipart(),
    multipart(dicom(StudyInstanceUID=None)), multipart(dicom(SeriesInstanceUID=None)),
    multipart(dicom(SOPInstanceUID=None)),
], ids=["empty", "not-dicom", "truncated", "no-parts", "no-study", "no-series", "no-sop"])
def test_malformed_uploads_denied(client, upstream, body):
    login_as(client, USER_CRISP)
    assert post(client, body).status_code == 400
    assert not upstream


def test_mismatched_file_meta_denied(client, upstream):
    login_as(client, USER_CRISP)
    body = dicom()
    ds = pydicom.dcmread(BytesIO(body))
    ds.SOPInstanceUID = generate_uid()
    out = BytesIO()
    pydicom.dcmwrite(out, ds)  # preserve mismatch
    assert post(client, multipart(out.getvalue())).status_code == 400
    assert not upstream


@pytest.mark.parametrize("body,content_type", [
    (multipart(dicom(sop_class="1.2.840.10008.5.1.4.1.1.2")), CONTENT_TYPE),
    (multipart(dicom()), "application/dicom"),
    (multipart(dicom()), 'multipart/related; type="application/dicom"'),
    (multipart(dicom()).replace(b"application/dicom\r", b"text/plain\r"), CONTENT_TYPE),
], ids=["ct", "singlepart", "missing-boundary", "non-dicom-part"])
def test_unsupported_upload_denied(client, upstream, body, content_type):
    login_as(client, USER_CRISP)
    assert post(client, body, content_type=content_type).status_code == 415
    assert not upstream


def test_admin_upload_requires_valid_dicom(client, upstream):
    login_as(client, TEST_USER)
    body = b"admin body passes directly to Orthanc"
    assert post(client, body).status_code == 400
    assert not upstream


def test_staff_has_same_annotation_access(client, upstream):
    from db import get_conn

    login_as(client, USER_CRISP)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_staff = TRUE WHERE username = %s", (USER_CRISP,))
        conn.commit()
        assert post(client, multipart(dicom(sop_class=SEG))).status_code == 200
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_staff = FALSE WHERE username = %s", (USER_CRISP,))
        conn.commit()
        conn.close()


@pytest.mark.parametrize("limit,value", [
    ("OHIF_ANNOTATION_MAX_UPLOAD_BYTES", 64), ("MAX_INSTANCES", 1), ("MAX_PART_HEADER_BYTES", 8),
])
def test_limits_deny_without_forwarding(client, upstream, monkeypatch, limit, value):
    login_as(client, USER_CRISP)
    monkeypatch.setattr(uploads, limit, value)
    assert post(client, multipart(dicom(), dicom())).status_code == 413
    assert not upstream


@pytest.mark.asyncio
async def test_chunk_boundaries_spooling_and_cleanup(monkeypatch):
    files = []

    def spool(**kwargs):
        f = SpooledTemporaryFile(**kwargs)
        files.append(f)
        return f

    monkeypatch.setattr(uploads, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(uploads, "SPOOL_MEMORY_BYTES", 32)
    monkeypatch.setattr(uploads, "CHUNK_BYTES", 7)
    monkeypatch.setattr(dataset_access, "get_study_datasets_cached", lambda uid: frozenset({"test"}))
    body = multipart(dicom(), dicom(sop_class=SEG, PixelData=b"\0" * 1024))
    chunks = iter([body[i:i+11] for i in range(0, len(body), 11)])

    async def receive():
        chunk = next(chunks, None)
        return {"type": "http.request", "body": chunk or b"", "more_body": chunk is not None}

    request = Request({
        "type": "http", "method": "POST", "path": "/dicom-web/studies", "query_string": b"",
        "headers": [(b"content-type", CONTENT_TYPE.encode())],
    }, receive)
    async with uploads.validated_upload(request, frozenset({"test"}), None) as (raw, size):
        assert size == len(body)
        assert raw.read() == body
        assert raw._rolled
    assert all(f.closed for f in files)


@pytest.mark.asyncio
async def test_disconnect_closes_spool(monkeypatch):
    files = []

    def spool(**kwargs):
        f = SpooledTemporaryFile(**kwargs)
        files.append(f)
        return f

    monkeypatch.setattr(uploads, "SpooledTemporaryFile", spool)

    async def receive():
        return {"type": "http.disconnect"}

    request = Request({
        "type": "http", "method": "POST", "path": "/dicom-web/studies", "query_string": b"",
        "headers": [(b"content-type", CONTENT_TYPE.encode())],
    }, receive)
    with pytest.raises(ClientDisconnect):
        async with uploads.validated_upload(request, frozenset({"test"}), None):
            pytest.fail("disconnected upload was accepted")
    assert files and all(f.closed for f in files)


def test_trailing_study_after_pixels_is_rejected(client, upstream):
    login_as(client, USER_CRISP)
    # An attacker could append an out-of-scope UID after the pixel data. A
    # stop_before_pixels validator would see only the permitted study.
    body = dicom(sop_class=SEG, PixelData=b"\0\0")
    uid = P2.encode() + b"\0"
    body += b"\x20\x00\x0d\x00UI" + len(uid).to_bytes(2, "little") + uid
    assert post(client, multipart(body)).status_code == 400
    assert not upstream


def test_fractional_segmentation_allowed(client, upstream):
    login_as(client, USER_CRISP)
    body = dicom(
        sop_class=SEG, SegmentationType="FRACTIONAL", SegmentationFractionalType="PROBABILITY",
        MaximumFractionalValue=255, Rows=2, Columns=2, NumberOfFrames="1", SamplesPerPixel=1,
        BitsAllocated=8, BitsStored=8, HighBit=7, PixelRepresentation=0,
        PhotometricInterpretation="MONOCHROME2", PixelData=b"\0\x80\xff\0",
    )
    assert post(client, multipart(body)).status_code == 200
    assert upstream[0][1] == multipart(body)


def test_deflated_dataset_rejected_before_inflation():
    ds = pydicom.dcmread(BytesIO(dicom()))
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.1.99"
    out = BytesIO()
    pydicom.dcmwrite(out, ds, enforce_file_format=True)
    with pytest.raises(uploads.HTTPException) as exc:
        uploads._study_uids(BytesIO(out.getvalue()))
    assert exc.value.status_code == 415


def test_large_segmentation_metadata_does_not_load_pixels(monkeypatch):
    body = dicom(sop_class=SEG, PixelData=b"\0" * (9 * 1024 * 1024), references=[P1])
    original_getitem = Dataset.__getitem__

    def getitem_without_pixels(self, tag):
        assert tag != 0x7FE00010, "deferred pixel data was loaded"
        return original_getitem(self, tag)

    monkeypatch.setattr(Dataset, "__getitem__", getitem_without_pixels)
    with SpooledTemporaryFile(max_size=uploads.SPOOL_MEMORY_BYTES) as part:
        part.write(body)
        assert part._rolled
        assert uploads._study_uids(part) == (P1, {P1})


@pytest.mark.asyncio
async def test_invalid_dicom_closes_all_spools(monkeypatch):
    files = []

    def spool(**kwargs):
        f = SpooledTemporaryFile(**kwargs)
        files.append(f)
        return f

    monkeypatch.setattr(uploads, "SpooledTemporaryFile", spool)
    body = multipart(b"invalid-dicom")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/dicom-web/studies", "query_string": b"",
        "headers": [(b"content-type", CONTENT_TYPE.encode())],
    }, receive)
    with pytest.raises(uploads.HTTPException) as exc:
        async with uploads.validated_upload(request, frozenset({"test"}), None):
            pytest.fail("invalid upload was accepted")
    assert exc.value.status_code == 400
    assert len(files) == 2 and all(f.closed for f in files)


@pytest.mark.parametrize("status", [202, 400, 409, 500])
def test_upstream_response_and_cleanup(client, monkeypatch, status):
    login_as(client, USER_CRISP)
    files = []

    def spool(**kwargs):
        f = SpooledTemporaryFile(**kwargs)
        files.append(f)
        return f

    async def handler(request):
        await request.aread()

        async def response():
            yield b"upstream-response"

        return httpx.Response(status, content=response())

    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", stub)
    monkeypatch.setattr(uploads, "SpooledTemporaryFile", spool)
    try:
        resp = post(client, multipart(dicom()))
        assert resp.status_code == status
        assert resp.content == b"upstream-response"
        assert files and all(f.closed for f in files)
    finally:
        asyncio.run(stub.aclose())


@pytest.mark.asyncio
async def test_chunked_limit_without_content_length_cleans_spool(monkeypatch):
    files = []

    def spool(**kwargs):
        f = SpooledTemporaryFile(**kwargs)
        files.append(f)
        return f

    monkeypatch.setattr(uploads, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(uploads, "OHIF_ANNOTATION_MAX_UPLOAD_BYTES", 4)

    async def receive():
        return {"type": "http.request", "body": b"12345", "more_body": True}

    request = Request({
        "type": "http", "method": "POST", "path": "/dicom-web/studies", "query_string": b"",
        "headers": [(b"content-type", CONTENT_TYPE.encode())],
    }, receive)
    with pytest.raises(uploads.HTTPException) as exc:
        async with uploads.validated_upload(request, frozenset({"test"}), None):
            pytest.fail("oversized upload was accepted")
    assert exc.value.status_code == 413
    assert files and all(f.closed for f in files)
