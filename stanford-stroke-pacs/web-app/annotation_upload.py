"""Bounded, dataset-scoped validation of OHIF Part 10 SR/SEG STOW uploads.

Nothing is sent to Orthanc until every part has passed validation. The original
wire body is retained separately so validation cannot alter the saved objects.
"""

from contextlib import asynccontextmanager
from tempfile import SpooledTemporaryFile

import pydicom
from fastapi import HTTPException, Request
from pydicom.filereader import data_element_generator, read_dataset, read_preamble
from pydicom.uid import UID
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool

import dataset_access
from config import OHIF_ANNOTATION_MAX_UPLOAD_BYTES

SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
MAX_INSTANCES = 64
MAX_PART_HEADER_BYTES = 16 * 1024
CHUNK_BYTES = 64 * 1024
ANNOTATION_SOP_CLASSES = frozenset({
    "1.2.840.10008.5.1.4.1.1.88.22",  # Enhanced SR
    "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
    "1.2.840.10008.5.1.4.1.1.88.34",  # Comprehensive 3D SR
    "1.2.840.10008.5.1.4.1.1.66.4",   # Segmentation (binary/fractional)
})
# OHIF writes native, uncompressed Part 10 datasets. Reject deflated datasets
# before dcmread can inflate them without a bound; no pixel decoding is needed.
NATIVE_TRANSFER_SYNTAXES = frozenset({
    "1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.2",
})


def _bad_request():
    return HTTPException(400, "Invalid annotation upload")


def _uid(value):
    if not isinstance(value, str) or not value or not UID(value).is_valid:
        raise _bad_request()
    return value


def _study_uids(part, instances=None):
    """Read metadata and references without loading SEG pixel data."""
    try:
        part.seek(0)
        read_preamble(part, force=False)
        meta = read_dataset(
            part, is_implicit_VR=False, is_little_endian=True,
            stop_when=lambda tag, _vr, _length: tag.group != 2,
        )
        if meta.get("TransferSyntaxUID") not in NATIVE_TRANSFER_SYNTAXES:
            raise HTTPException(415, "Unsupported annotation transfer syntax")
        # Reject unordered/duplicate top-level tags rather than depending on
        # different DICOM parsers agreeing on which repeated UID wins. Also
        # inspect metadata after PixelData; stopping before pixels would let a
        # forged trailing StudyInstanceUID escape authorization.
        syntax = UID(meta.TransferSyntaxUID)
        previous = -1
        for elem in data_element_generator(
            part, is_implicit_VR=syntax.is_implicit_VR,
            is_little_endian=syntax.is_little_endian, defer_size=1024 * 1024,
        ):
            if elem.tag <= previous:
                raise _bad_request()
            previous = elem.tag
        part.seek(0)
        ds = pydicom.dcmread(part, defer_size=1024 * 1024)
        study = _uid(ds.get("StudyInstanceUID"))
        _uid(ds.get("SeriesInstanceUID"))
        sop = _uid(ds.get("SOPInstanceUID"))
        sop_class = _uid(ds.get("SOPClassUID"))
        if (ds.file_meta.get("MediaStorageSOPInstanceUID") != sop
                or ds.file_meta.get("MediaStorageSOPClassUID") != sop_class):
            raise _bad_request()
        if sop_class not in ANNOTATION_SOP_CLASSES:
            raise HTTPException(415, "Only SR and SEG annotations may be uploaded")
        # Evidence/reference sequences can carry additional studies. Do not
        # authorize these using the top-level StudyInstanceUID alone.
        if instances is not None:
            instances.append({"study": study, "series": str(ds.SeriesInstanceUID),
                              "sop": sop, "sr": sop_class != "1.2.840.10008.5.1.4.1.1.66.4"})
        studies = set()

        def collect_studies(dataset):
            # Iterating .iterall() would load deferred PixelData as a side effect.
            for tag in dataset.keys():
                if tag == 0x0020000D:
                    studies.add(_uid(dataset[tag].value))
                elif tag not in (0x7FE00010, 0x7FE00008, 0x7FE00009) and dataset[tag].VR == "SQ":
                    for item in dataset[tag].value:
                        collect_studies(item)

        collect_studies(ds)
        return study, studies
    except HTTPException:
        raise
    except Exception:
        # Parser exceptions can contain patient metadata. Return a fixed message.
        raise _bad_request() from None


def _validate(raw, boundary, scope, requested_uids):
    part = None
    count = 0
    ended = False
    header_bytes = 0
    field = bytearray()
    value = bytearray()
    headers = {}
    studies = set()
    instances = []

    def on_part_begin():
        nonlocal part, count, header_bytes, headers
        count += 1
        if count > MAX_INSTANCES:
            raise HTTPException(413, "Too many annotation instances")
        part = SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES)
        header_bytes = 0
        headers = {}

    def header_data(target, data, start, end):
        nonlocal header_bytes
        header_bytes += end - start
        if header_bytes > MAX_PART_HEADER_BYTES:
            raise HTTPException(413, "Annotation part headers too large")
        target.extend(data[start:end])

    def on_header_end():
        key = bytes(field).lower()
        if key in headers:
            raise _bad_request()
        headers[key] = bytes(value)
        field.clear()
        value.clear()

    def on_headers_finished():
        content_type, _params = parse_options_header(headers.get(b"content-type", b""))
        if content_type.lower() != b"application/dicom" or b"content-transfer-encoding" in headers:
            raise HTTPException(415, "Annotation parts must be application/dicom")

    def on_part_end():
        nonlocal part
        study, referenced_studies = _study_uids(part, instances)
        if any(uid != study for uid in requested_uids):
            raise _bad_request()
        studies.update(referenced_studies)
        part.close()
        part = None

    def on_end():
        nonlocal ended
        ended = True

    parser = MultipartParser(boundary, {
        "on_part_begin": on_part_begin,
        "on_header_field": lambda data, start, end: header_data(field, data, start, end),
        "on_header_value": lambda data, start, end: header_data(value, data, start, end),
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
        "on_part_data": lambda data, start, end: part.write(data[start:end]),
        "on_part_end": on_part_end,
        "on_end": on_end,
    })
    try:
        raw.seek(0)
        while chunk := raw.read(CHUNK_BYTES):
            parser.write(chunk)
        parser.finalize()
        if not ended or not count:
            raise _bad_request()
    except HTTPException:
        raise
    except Exception:
        raise _bad_request() from None
    finally:
        if part is not None:
            part.close()
    for study in studies:
        if not dataset_access.scope_allows(scope, dataset_access.get_study_datasets_cached(study)):
            raise HTTPException(403, "Dataset access denied")
    raw.seek(0)
    return instances


@asynccontextmanager
async def validated_upload(request: Request, scope, path_uid):
    """Yield a rewindable original request body only after full authorization."""
    if scope is not None and not scope:
        raise HTTPException(403, "Dataset access denied")
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary", b"")
    if (content_type.lower() != b"multipart/related"
            or params.get(b"type", b"").lower() != b"application/dicom"
            or not boundary or len(boundary) > 70
            or request.headers.get("content-encoding", "identity").lower() != "identity"):
        raise HTTPException(415, "Expected multipart/related application/dicom")
    requested_uids = [path_uid] if path_uid else []
    for key in ("StudyInstanceUID", "0020000D"):
        requested_uids.extend(request.query_params.getlist(key))
    for uid in requested_uids:
        _uid(uid)
    raw = SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES)
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > OHIF_ANNOTATION_MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Annotation upload too large")
            await run_in_threadpool(raw.write, chunk)
        request.state.annotation_instances = await run_in_threadpool(
            _validate, raw, boundary, scope, requested_uids)
        yield raw, size
    finally:
        await run_in_threadpool(raw.close)
