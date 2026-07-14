import pydicom
import SimpleITK as sitk


def name_sanity_check(name):
    if isinstance(name, str):
        name = name.replace("/", "_")
        name = name.replace("*", " ")
        name = name.replace(":", " ")
        name = name.replace("?", " ")
        name = name.replace('"', " ")
        name = name.replace("<", " ")
        name = name.replace(">", " ")
        name = name.replace("'", " ")
    return str(name)

def anonymize_dicom_slice(dcm, study_id=None):
    if study_id is None:
        try:
            study_id = str(dcm["PatientID"].value)
        except Exception:
            study_id = "1"

    # Anonimyze DICOM slices while preserving the Stanford study identifier.
    changed_elements_with_values = [('InstitutionAddress', ''),
                                    ('InstitutionName', ''),
                                    ('PatientAge', ''),
                                    ('PatientBirthDate', ''),
                                    ('PatientID', str(study_id)),
                                    ('PatientName', 'Anonymous'),
                                    ('PatientSex', ''),
                                    ('ReferringPhysicianName', ''),
                                    ('StationName', ''),
                                    ('AccessionNumber', ''),
                                    ('DeviceSerialNumber', ''),
                                    ('ProtocolName', ''),
                                    ('StudyID', str(study_id)),
                                    ('ImageComments', '')]

    # Change tags to anonymized values
    for element, new_value in changed_elements_with_values:
        try:
            dcm[element].value = new_value
        except Exception:
            pass
    # Add additional tags for anonymized dicoms
    dcm.add_new(pydicom.tag.Tag(0x00120063), "LO", "BASIC APPLICATION LEVEL CONFIDENTIALITY PROFILE")
    dcm.add_new(pydicom.tag.Tag(0x00120062), "CS", "YES")

    return dcm

# --- Geometric series-type detection (CTP / PWI / DWI) -----------------------
#
# The discriminating signal is `same_position_count`: how many frames in a
# series share the same ImagePositionPatient. Static scans (CTA/NCCT) visit each
# slice location once (~1); dynamic acquisitions cycle through time/b-values at
# each location, so the count equals the number of timepoints. Combined with
# Modality (CT->CTP, MR->PWI/DWI) and a small SeriesDescription exclusion list,
# this cleanly separates the perfusion/diffusion families. See
# max_same_position_count() for how the count is derived from DICOM headers.

# Frame-count thresholds, from the reference implementation's call sites
# (get_metadata.py): perf_identifier(n_same_pos=(14, 1e6)),
# dwi_identifier(n_same_pos=(2, 14)).
#
# CTP uses his floor of 14 exactly. PWI cannot: his two ranges OVERLAP at 14, and
# he can afford that because his output is five INDEPENDENT columns (a 14-frame MR
# series is simply flagged in both likely_dwi and likely_pwi). We emit one
# mutually-exclusive series_type, so the tie has to break somewhere — it breaks to
# DWI, since 14 is the top of his stated DWI range and the bottom of his perfusion
# range. 2 MR series in the live corpus sit on that boundary.
CTP_MIN_FRAMES = 14                 # CT: his floor, used as-is
PWI_MIN_FRAMES = 15                 # MR: 14 belongs to DWI (see above)
DWI_FRAME_RANGE = (2, 14)           # his dwi_identifier call
MR_DYNAMIC_EXCLUDE = ("asl", "fmri", "qsm", "swi")


def _position_key(position):
    """Round an ImagePositionPatient triple to a hashable key, or None.

    Accepts list/tuple and pydicom MultiValue (a non-list sequence of DSfloat).
    """
    if position is None or isinstance(position, (str, bytes)):
        return None
    try:
        if len(position) < 3:
            return None
        return tuple(round(float(position[i]), 2) for i in range(3))
    except (TypeError, ValueError):
        return None


def _frame_positions(dcm):
    """Best-effort per-frame ImagePositionPatient for enhanced/multiframe DICOM.

    Returns a list of position keys (one per frame), or None when the
    PerFrameFunctionalGroupsSequence / PlanePositionSequence structure is
    absent or unreadable — in which case the caller degrades to None rather
    than guessing.
    """
    per_frame = getattr(dcm, "PerFrameFunctionalGroupsSequence", None)
    if not per_frame:
        return None
    keys = []
    for frame in per_frame:
        plane = getattr(frame, "PlanePositionSequence", None)
        if not plane:
            return None
        key = _position_key(getattr(plane[0], "ImagePositionPatient", None))
        if key is None:
            return None
        keys.append(key)
    return keys or None


def max_same_position_count(headers):
    """Largest number of frames sharing one ImagePositionPatient in a series.

    ~1 for static scans (CTA/NCCT), ~N_timepoints for dynamic acquisitions
    (CTP/PWI/DWI). `headers` is the list of pydicom datasets the pipeline
    already holds for a series (read with stop_before_pixels=True).

    Returns None when no positions are available, or when an enhanced-multiframe
    series carries geometry we cannot decode — so callers degrade to an
    undetermined series_type instead of misclassifying.
    """
    if not headers:
        return None
    counts = {}
    seen = False
    for dcm in headers:
        try:
            n_frames = int(getattr(dcm, "NumberOfFrames", 0) or 0)
        except (TypeError, ValueError):
            n_frames = 0
        if n_frames > 1:
            # Enhanced multiframe: a single file holds many frames. Per-file
            # ImagePositionPatient under-counts, so read per-frame positions.
            frame_keys = _frame_positions(dcm)
            if frame_keys is None:
                return None  # multiframe geometry we can't read -> don't guess
            for key in frame_keys:
                counts[key] = counts.get(key, 0) + 1
                seen = True
            continue
        key = _position_key(getattr(dcm, "ImagePositionPatient", None))
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
        seen = True
    if not seen:
        return None
    return max(counts.values())


def _description_excluded(seriesdescription, tokens=MR_DYNAMIC_EXCLUDE):
    """True if the description names a non-target MR series (asl/fmri/qsm/swi)."""
    if not isinstance(seriesdescription, str):
        return False
    text = seriesdescription.lower()
    return any(token in text for token in tokens)


def is_ctp_series(modality, same_position_count, seriesdescription=None):
    """CT perfusion: CT with many time frames per slice location."""
    return (
        modality == "CT"
        and same_position_count is not None
        and same_position_count >= CTP_MIN_FRAMES
    )


def is_pwi_series(modality, same_position_count, seriesdescription=None):
    """MR perfusion (DSC/DCE): MR with many time frames per slice location."""
    return (
        modality == "MR"
        and same_position_count is not None
        and same_position_count >= PWI_MIN_FRAMES
        and not _description_excluded(seriesdescription)
    )


def is_dwi_series(modality, same_position_count, seriesdescription=None):
    """Diffusion: MR with a few frames (b-values/directions) per slice location."""
    lo, hi = DWI_FRAME_RANGE
    return (
        modality == "MR"
        and same_position_count is not None
        and lo <= same_position_count <= hi
        and not _description_excluded(seriesdescription)
    )


def identify_series_type(modality, same_position_count, seriesdescription=None):
    """Geometry-first series-type detection. Returns 'CTP'/'PWI'/'DWI'/None.

    Deliberately narrow: it answers only "is this a dynamic acquisition?", which
    geometry alone can decide. The static families (CTA/NCCT/bone/dual-energy)
    need kernels and descriptions — see series_classification.classify_series,
    which calls this as its geometry stage.
    """
    if is_ctp_series(modality, same_position_count, seriesdescription):
        return "CTP"
    if is_pwi_series(modality, same_position_count, seriesdescription):
        return "PWI"
    if is_dwi_series(modality, same_position_count, seriesdescription):
        return "DWI"
    return None


# Series types that auto-convert to NIfTI at ingest, in `legacy` storage mode.
# EMPTY BY DESIGN — auto-NIfTI stays dormant; on-demand conversion lives in
# scripts/dicom/dicom_to_nifti.py.
#
# This used to be implicitly dormant: should_create_nifti() asked for CTA/NCCT
# and identify_series_type() could not emit them. series_classification now DOES
# emit CTA/NCCT, so that accident is gone — the dormancy is stated here instead.
# Populate this set to switch auto-NIfTI on deliberately.
NIFTI_SERIES_TYPES: frozenset[str] = frozenset()


def should_create_nifti(series_type):
    return series_type in NIFTI_SERIES_TYPES


def convert_dicom_to_nifti(input_path, output_path):
    """Convert a DICOM series directory to a NIfTI file.

    Live: consumed by scripts/dicom/dicom_to_nifti.py — keep name/signature
    stable.
    """
    reader = sitk.ImageSeriesReader()
    dicom_names = reader.GetGDCMSeriesFileNames(input_path)
    reader.SetFileNames(dicom_names)
    image = reader.Execute()
    # image = sitk.PermuteAxes(image, [2, 1, 0])
    sitk.WriteImage(image, output_path)
