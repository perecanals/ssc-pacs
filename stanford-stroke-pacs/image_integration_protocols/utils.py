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


def is_ctp_series(seriesdescription):
    ctp_prefixes = ["CTP", "PERF", "PERFUSION", "CT PERFUSION"]
    return isinstance(seriesdescription, str) and seriesdescription.startswith(tuple(ctp_prefixes))


def identify_series_type(seriesdescription):
    if is_cta_series(seriesdescription):
        return "CTA"
    if is_ncct_series(seriesdescription):
        return "NCCT"
    if is_ctp_series(seriesdescription):
        return "CTP"
    return ""


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