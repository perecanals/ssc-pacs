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

# --- Anonymisation -----------------------------------------------------------
#
# Header-level de-identification applied when the YAML sets `anonymize_files`.
# The tag set is the one the legacy FlowCat protocol used (so a corpus that
# already went through it stays homogeneous) plus the remaining person/physician
# identifiers of PS3.15 Table E.1-1 that the pipeline never reads. Deliberately
# NOT touched, because the pipeline depends on them: every UID (Study/Series/SOP/
# FrameOfReference — the whole DB keys on them), StudyDate/Time and the
# acquisition timestamps (acquisitiondatetime, timepoints), StudyDescription
# (study_type) and SeriesDescription (directory name, series_type), plus every
# geometry / reconstruction tag the classifier reads. Private tags are kept by
# default: `series_dicom_tags` stores them on purpose for vendor-specific
# discrimination (see dicom_tags.py). This is header tidying, not pixel
# redaction — burned-in annotations are out of scope.

ANONYMIZE_BLANK_KEYWORDS = (
    "InstitutionAddress",
    "InstitutionName",
    "InstitutionalDepartmentName",
    "PatientAge",
    "PatientBirthDate",
    "PatientSex",
    "PatientWeight",
    "PatientSize",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "OtherPatientIDs",
    "OtherPatientNames",
    "IssuerOfPatientID",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
    "OperatorsName",
    "RequestingPhysician",
    "PhysiciansOfRecord",
    "NameOfPhysiciansReadingStudy",
    "StationName",
    "AccessionNumber",
    "DeviceSerialNumber",
    "ProtocolName",
    "ImageComments",
    "StudyComments",
    "AdditionalPatientHistory",
)

# Sequences that carry identifiers and cannot be meaningfully blanked.
ANONYMIZE_DELETE_KEYWORDS = (
    "RequestAttributesSequence",
    "OtherPatientIDsSequence",
    "ReferencedPatientSequence",
)

ANONYMIZE_PATIENT_NAME = "Anonymous"
ANONYMIZE_METHOD = "BASIC APPLICATION LEVEL CONFIDENTIALITY PROFILE"


def anonymize_dicom_slice(dcm, study_id=None, *, remove_private_tags=False):
    """De-identify a pydicom Dataset in place and return it.

    ``study_id`` becomes both PatientID and StudyID (created when absent). When
    it is not given the dataset's own PatientID is kept. Blank-list elements
    are emptied only if present (no empty elements are added); delete-list
    sequences are removed; UIDs, dates and descriptions are preserved. Calling
    it twice is a no-op the second time.
    """
    if study_id is None:
        try:
            study_id = str(dcm["PatientID"].value)
        except Exception:
            study_id = "1"
    study_id = str(study_id)

    for keyword in ANONYMIZE_BLANK_KEYWORDS:
        if keyword in dcm:
            dcm[keyword].value = ""

    for keyword in ANONYMIZE_DELETE_KEYWORDS:
        if keyword in dcm:
            del dcm[keyword]

    # setattr creates the element with the dictionary VR when it is absent —
    # the identity tags must exist for the pipeline (PatientID is mandatory).
    dcm.PatientID = study_id
    dcm.StudyID = study_id
    dcm.PatientName = ANONYMIZE_PATIENT_NAME
    dcm.PatientIdentityRemoved = "YES"
    dcm.DeidentificationMethod = ANONYMIZE_METHOD

    if remove_private_tags:
        dcm.remove_private_tags()

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


def convert_dicom_to_nifti(input_path, output_path, series_uid=None):
    """Convert a DICOM series directory to a NIfTI file.

    Live: consumed by scripts/dicom/dicom_to_nifti.py — keep name/signature
    stable.
    """
    reader = sitk.ImageSeriesReader()
    series_ids = reader.GetGDCMSeriesIDs(input_path) or []
    if series_uid is not None and series_uid not in series_ids:
        raise ValueError("Requested DICOM series was not found in the input directory")
    if series_uid is None and len(series_ids) != 1:
        raise ValueError("Input must contain exactly one DICOM series, or specify its UID")
    dicom_names = reader.GetGDCMSeriesFileNames(input_path, series_uid or series_ids[0])
    reader.SetFileNames(dicom_names)
    image = reader.Execute()
    # image = sitk.PermuteAxes(image, [2, 1, 0])
    sitk.WriteImage(image, output_path)
