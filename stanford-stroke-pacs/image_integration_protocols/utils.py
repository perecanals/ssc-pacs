import SimpleITK as sitk
import pydicom

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

def identify_study_type(study_series):
    # CTA group prefixes -> To identify baseline studies
    cta_prefixes = ["CTA", "Angio  ", "ANGIO TSA", "Angio TSA", "Angio Tsa", "AngioTC ", "CTA P.WILLIS 0.6 H20f", "TSA 0,60 Hv40 A2", "TRONCOS SUPRAAORTICOS + CEREBRAL 1,00 Bv36 S3", 'TSA+WILLIS']
    # NCCT group prefixes -> To identify potential follow-up studies
    ncct_follow_up_prefixes = ["CEREBRAL ", "NCCT  ", "CRANEO  "]
    # Thrombectomy -> To identify thrombectomy studies
    thrombectomy_study_names_prefixes = ["Trombectomia"]
    thrombectomy_study_names_suffixes = ["Cabeza"]
    """Identify the type of a given study (we assume non-anonymized dicom series only!)"""
    study_description_col = "studydescription" if "studydescription" in study_series.columns else "StudyDescription"
    series_description_col = "seriesdescription" if "seriesdescription" in study_series.columns else "SeriesDescription"

    # Check StudyDescription for non-anonymized series to find Thrombectomy studies
    first_study_description = study_series[study_description_col].iloc[0]
    if isinstance(first_study_description, str) and (
        first_study_description.startswith(tuple(thrombectomy_study_names_prefixes))
        or first_study_description.endswith(tuple(thrombectomy_study_names_suffixes))
    ):
        return "THROMBECTOMY"
    # For the rest, check SeriesDescription for non-anonymized series
    for description in study_series[series_description_col]:
        if is_cta_series(description):
            return "BASELINE"
    for description in study_series[series_description_col]:
        if is_ncct_series(description):
            return "FOLLOW_UP"
    return None

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
        except:
            pass
    # Add additional tags for anonymized dicoms
    dcm.add_new(pydicom.tag.Tag(0x00120063), "LO", "BASIC APPLICATION LEVEL CONFIDENTIALITY PROFILE")
    dcm.add_new(pydicom.tag.Tag(0x00120062), "CS", "YES")

    return dcm

def is_cta_series(seriesdescription):
    cta_prefixes = [
        "CTA",
        "Angio  ",
        "ANGIO TSA",
        "Angio TSA",
        "Angio Tsa",
        "AngioTC ",
        "TSA 0,60 Hv40 ",
    ]
    return isinstance(seriesdescription, str) and seriesdescription.startswith(tuple(cta_prefixes))


def is_ncct_series(seriesdescription):
    ncct_prefixes = ["CEREBRAL ", "NCCT  ", "CRANEO  "]
    return isinstance(seriesdescription, str) and seriesdescription.startswith(tuple(ncct_prefixes))


# --- Geometric series-type detection (CTP / PWI / DWI) -----------------------
#
# The discriminating signal is `same_position_count`: how many frames in a
# series share the same ImagePositionPatient. Static scans (CTA/NCCT) visit each
# slice location once (~1); dynamic acquisitions cycle through time/b-values at
# each location, so the count equals the number of timepoints. Combined with
# Modality (CT->CTP, MR->PWI/DWI) and a small SeriesDescription exclusion list,
# this cleanly separates the perfusion/diffusion families. See
# max_same_position_count() for how the count is derived from DICOM headers.

PERFUSION_MIN_FRAMES = 15            # CTP and PWI floor (frames per slice)
DWI_FRAME_RANGE = (2, 14)           # below the perfusion floor -> no overlap
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
        and same_position_count >= PERFUSION_MIN_FRAMES
    )


def is_pwi_series(modality, same_position_count, seriesdescription=None):
    """MR perfusion (DSC/DCE): MR with many time frames per slice location."""
    return (
        modality == "MR"
        and same_position_count is not None
        and same_position_count >= PERFUSION_MIN_FRAMES
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

    CTA/NCCT keyword detection is intentionally NOT used here — those keyword
    lists were never tuned for the current dataset. is_cta_series/is_ncct_series
    remain available for identify_study_type's BASELINE/FOLLOW_UP classification.
    """
    if is_ctp_series(modality, same_position_count, seriesdescription):
        return "CTP"
    if is_pwi_series(modality, same_position_count, seriesdescription):
        return "PWI"
    if is_dwi_series(modality, same_position_count, seriesdescription):
        return "DWI"
    return None


def should_create_nifti(series_type):
    return series_type in {"CTA", "NCCT"}

def convert_dicom_to_nifti(input_path, output_path):
    """
    Passes dicom series to nifti.

    Parameters
    ----------
    input_path : string
        Path to the dicom series.
    output_path : string
        Path where final nifti will be stored.

    Returns
    -------

    """
    reader = sitk.ImageSeriesReader()
    dicom_names = reader.GetGDCMSeriesFileNames(input_path)
    reader.SetFileNames(dicom_names)
    image = reader.Execute()
    # image = sitk.PermuteAxes(image, [2, 1, 0])
    sitk.WriteImage(image, output_path)