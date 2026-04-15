import os
import SimpleITK as sitk
import pydicom
import pandas as pd

def create_series_table(case_dir):
    data_series_list = []

    def safe_text(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode(errors="ignore")
        value = str(value).strip()
        return value if value else None

    def safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def safe_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def safe_float_array(value):
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            value = [value]
        result = [safe_float(item) for item in value]
        return result if any(item is not None for item in result) else None

    def dicom_value(dataset, tag_name):
        if tag_name not in dataset:
            return None
        return dataset[tag_name].value

    def parse_datetime(dataset):
        for date_tag, time_tag in [
            ("AcquisitionDate", "AcquisitionTime"),
            ("StudyDate", "StudyTime"),
        ]:
            date_value = safe_text(dicom_value(dataset, date_tag))
            time_value = safe_text(dicom_value(dataset, time_tag)) or "000000"
            if date_value:
                parsed = pd.to_datetime(
                    f"{date_value}{time_value.split('.')[0]}",
                    format="%Y%m%d%H%M%S",
                    errors="coerce",
                )
                if pd.notna(parsed):
                    return parsed
        return pd.NaT

    for root, _, files in os.walk(case_dir):
        visible_files = sorted(filename for filename in files if not filename.startswith("."))
        if not visible_files:
            continue

        headers = []
        for filename in visible_files:
            filepath = os.path.join(root, filename)
            try:
                headers.append(pydicom.dcmread(filepath, stop_before_pixels=True))
            except Exception:
                continue

        if not headers:
            continue

        dcm = headers[0]
        patient_id = safe_text(dicom_value(dcm, "PatientID"))
        study_instance_uid = safe_text(dicom_value(dcm, "StudyInstanceUID"))
        series_instance_uid = safe_text(dicom_value(dcm, "SeriesInstanceUID"))
        if patient_id is None or study_instance_uid is None or series_instance_uid is None:
            continue

        rows = safe_int(dicom_value(dcm, "Rows"))
        cols = safe_int(dicom_value(dcm, "Columns"))
        slice_thickness = safe_float(dicom_value(dcm, "SliceThickness"))
        image_shape = [rows, cols, len(headers)] if rows and cols else None

        scan_axial_coverage_mm = None
        z_positions = []
        for header in headers:
            position = dicom_value(header, "ImagePositionPatient")
            if isinstance(position, (list, tuple)) and len(position) >= 3:
                z_value = safe_float(position[2])
                if z_value is not None:
                    z_positions.append(z_value)
        if len(z_positions) > 1 and slice_thickness is not None:
            scan_axial_coverage_mm = max(z_positions) - min(z_positions) + slice_thickness
        elif slice_thickness is not None:
            scan_axial_coverage_mm = slice_thickness * len(headers)

        data_series_list.append(
            pd.DataFrame(
                [
                    {
                        "patient_id": patient_id,
                        "acquisitiondatetime": parse_datetime(dcm),
                        "studydescription": safe_text(dicom_value(dcm, "StudyDescription")),
                        "seriesdescription": safe_text(dicom_value(dcm, "SeriesDescription")),
                        "studyinstanceuid": study_instance_uid,
                        "seriesinstanceuid": series_instance_uid,
                        "src_dicom_dir_path": root,
                        "protocolname": safe_text(dicom_value(dcm, "ProtocolName")),
                        "seriesnumber": safe_int(dicom_value(dcm, "SeriesNumber")),
                        "instancenumber": safe_int(dicom_value(dcm, "InstanceNumber")),
                        "manufacturer": safe_text(dicom_value(dcm, "Manufacturer")),
                        "pixelspacing": safe_float_array(dicom_value(dcm, "PixelSpacing")),
                        "slicethickness": slice_thickness,
                        "imageshape": image_shape,
                        "scanaxialcoverage_mm": scan_axial_coverage_mm,
                    }
                ]
            )
        )

    if not data_series_list:
        return pd.DataFrame()

    return pd.concat(data_series_list, ignore_index=True)

# series_table_raw = create_series_table(os.path.join(source_dir, nhc))
# series_table_raw = series_table_raw.sort_values(by=["nhc", "StudyDateTime"]).reset_index(drop=True)
# series_table_raw["seriesdescription_"] = series_table_raw["SeriesDescription"]
# series_table_raw["seriesdescription_"] = series_table_raw["seriesdescription_"].apply(name_sanity_check)

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