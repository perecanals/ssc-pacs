"""Per-series DICOM tag extraction for the `series_dicom_tags` table.

Pure library: takes the pydicom datasets a caller already holds and returns a
JSON-serializable dict. No I/O, no DB, no pandas — so both the ingestion
pipeline (which reads every header anyway) and the out-of-repo backfill (which
streams them out of `*.tar.zst`) can share one implementation.

Scope is deliberately *everything* on a representative instance: all standard
top-level elements keyed by their pydicom **keyword**, private elements under a
`_private` sub-dict, and sequences recursed to a shallow depth. Patient
identifiers are included — this DB already stores identified upstream data and a
curated allowlist would silently drop the vendor tags that distinguish
dual-energy / material-decomposition series (see the 0015 migration docstring).

Cross-instance aggregates are computed over the whole series, because no single
instance carries them: `same_position_count` (the CTP/PWI/DWI discriminator),
`n_positions`, and the distinct `ConvolutionKernel` / `ImageType` values, which
catch series whose recon parameters vary mid-acquisition.
"""

from __future__ import annotations

from typing import Any

import pydicom
from pydicom.multival import MultiValue
from pydicom.valuerep import PersonName

from utils import _frame_positions, _position_key

# Bump when the extracted shape changes in a way that makes old rows
# non-comparable; written to series_dicom_tags.extractor_version.
EXTRACTOR_VERSION = "tags-v1"

# Sequences are recursed to this depth. Everything observed in the corpus is a
# small code sequence (ProcedureCodeSequence, AnatomicRegionSequence,
# MultienergyCTAcquisitionSequence...), so 2 captures them whole without risking
# an unbounded walk.
MAX_SEQUENCE_DEPTH = 2

# Per-frame functional groups can hold thousands of items on enhanced multiframe
# instances. Keep only this many, so the jsonb stays bounded.
MAX_SEQUENCE_ITEMS = 2

# Carries no diagnostic signal and would dominate the payload.
SKIP_KEYWORDS = frozenset({"PixelData", "FloatPixelData", "DoubleFloatPixelData"})


def _coerce(value: Any, depth: int) -> Any:
    """Convert a pydicom element value into something json.dumps can handle."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, PersonName):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        # Binary payloads (thumbnails, LUT data, unparsed private blobs) — the
        # bytes themselves are not queryable, so record only that they existed.
        return None
    if isinstance(value, pydicom.sequence.Sequence):
        if depth >= MAX_SEQUENCE_DEPTH:
            return None
        return [_dataset_to_dict(item, depth + 1) for item in value[:MAX_SEQUENCE_ITEMS]]
    if isinstance(value, (MultiValue, list, tuple)):
        return [_coerce(v, depth) for v in value]
    # DSfloat / IS / DSdecimal and friends are numeric subclasses in disguise.
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _dataset_to_dict(dataset: pydicom.Dataset, depth: int = 0) -> dict:
    """Flatten one dataset to {keyword: value}, private elements under `_private`."""
    out: dict[str, Any] = {}
    private: dict[str, Any] = {}

    for elem in dataset:
        try:
            if elem.tag.is_private:
                key = f"{elem.tag.group:04X},{elem.tag.element:04X}"
                private[key] = _coerce(elem.value, depth)
                continue

            keyword = elem.keyword
            if not keyword or keyword in SKIP_KEYWORDS:
                # Unknown/retired public tags have no keyword — fall back to the
                # tag number rather than dropping them silently.
                if not keyword:
                    out[f"{elem.tag.group:04X},{elem.tag.element:04X}"] = _coerce(
                        elem.value, depth
                    )
                continue

            out[keyword] = _coerce(elem.value, depth)
        except Exception:
            # A single malformed element must not cost us the whole series.
            continue

    if private:
        out["_private"] = private
    return out


def _kernel_str(value: Any) -> str | None:
    """ConvolutionKernel is a str on some scanners and a MultiValue on others."""
    if value is None:
        return None
    if isinstance(value, (MultiValue, list, tuple)):
        return "".join(str(v) for v in value) or None
    text = str(value).strip()
    return text or None


class SeriesTagAccumulator:
    """Fold a series' instances into one tag row, one instance at a time.

    Memory is O(1) in the instance count: only the first dataset (the
    representative instance) is retained, everything else collapses into
    counters and sets as it goes. A 2,000-instance CTP series costs the same
    as a 20-slice NCCT.

    That matters because the backfill fans out across worker processes: holding
    every dataset of a large series resident, times N workers, is enough to OOM
    the host — which is exactly what happened on the first full run.

    `same_position_count` reproduces utils.max_same_position_count's semantics
    exactly, including its refusal to guess: if any enhanced-multiframe instance
    carries per-frame geometry we cannot decode, the count degrades to None for
    the whole series rather than under-counting.
    """

    def __init__(self, source_instance: str | None = None) -> None:
        self.source_instance = source_instance
        self._tags: dict | None = None
        self._position_counts: dict = {}
        self._positions: set = set()
        self._kernels: set[str] = set()
        self._image_types: set[str] = set()
        self._n_instances = 0
        self._saw_position = False
        self._geometry_undecodable = False

    def add(self, dcm) -> None:
        self._n_instances += 1

        if self._tags is None:
            self._tags = _dataset_to_dict(dcm)
            if self.source_instance is None:
                self.source_instance = getattr(dcm, "filename", None)

        kernel = _kernel_str(getattr(dcm, "ConvolutionKernel", None))
        if kernel:
            self._kernels.add(kernel)

        image_type = getattr(dcm, "ImageType", None)
        if isinstance(image_type, str) and image_type:
            self._image_types.add(image_type)
        elif image_type is not None:
            try:
                self._image_types.add("/".join(str(v) for v in image_type))
            except TypeError:
                pass

        self._add_positions(dcm)

    def _add_positions(self, dcm) -> None:
        if self._geometry_undecodable:
            return
        try:
            n_frames = int(getattr(dcm, "NumberOfFrames", 0) or 0)
        except (TypeError, ValueError):
            n_frames = 0

        if n_frames > 1:
            # Enhanced multiframe: one file holds many frames, so the per-file
            # ImagePositionPatient under-counts. Read the per-frame positions.
            frame_keys = _frame_positions(dcm)
            if frame_keys is None:
                self._geometry_undecodable = True
                return
            for key in frame_keys:
                self._position_counts[key] = self._position_counts.get(key, 0) + 1
                self._positions.add(key)
                self._saw_position = True
            return

        key = _position_key(getattr(dcm, "ImagePositionPatient", None))
        if key is None:
            return
        self._position_counts[key] = self._position_counts.get(key, 0) + 1
        self._positions.add(key)
        self._saw_position = True

    def result(self) -> dict:
        if self._geometry_undecodable or not self._saw_position:
            same_position_count = None
        else:
            same_position_count = max(self._position_counts.values())

        return {
            "tags": self._tags or {},
            "same_position_count": same_position_count,
            "n_positions": len(self._positions) or None,
            "n_instances_scanned": self._n_instances,
            "distinct_kernels": sorted(self._kernels),
            "distinct_image_types": sorted(self._image_types),
            "source_instance": self.source_instance,
            "extractor_version": EXTRACTOR_VERSION,
        }


def extract_series_tags(headers: list, source_instance: str | None = None) -> dict:
    """Build one `series_dicom_tags` row from a series' instance headers.

    Convenience wrapper over SeriesTagAccumulator for callers that already hold
    the datasets in memory (the ingestion pipeline reads them all anyway). The
    backfill streams instead, feeding the accumulator directly — but both go
    through the same fold, so they cannot disagree.
    """
    accumulator = SeriesTagAccumulator(source_instance=source_instance)
    for dcm in headers:
        accumulator.add(dcm)
    return accumulator.result()
