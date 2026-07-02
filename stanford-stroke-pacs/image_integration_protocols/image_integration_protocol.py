import json
import os
import shutil
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pydicom
import zstandard as zstd
from sqlalchemy import MetaData, Table, func, inspect, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from utils import (
    anonymize_dicom_slice,
    convert_dicom_to_nifti,
    identify_study_type,
    identify_series_type,
    max_same_position_count,
    name_sanity_check,
    should_create_nifti,
)

# Pull canonical paths from repo-root config.toml (same source of truth the
# the web app uses). Avoids hardcoding /DATA2/pacs_imaging_data here.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_WEB_APP_DIR = _REPO_ROOT / "web-app"
if str(_WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_APP_DIR))
from config import DICOM_DATA_ROOT, STORAGE_MODE  # noqa: E402

import warnings
warnings.filterwarnings(
    "ignore",
    message=r"The value length .* exceeds the maximum length of .* allowed for VR LO\.",
)


class ImageIntegrationProtocol:
    def __init__(
        self,
        case_dir,
        postgres_engine,
        anonymize_files=False,
        delete_originals_after_verification=False,
        import_id=None,
        import_label=None,
        dataset=None,
        cold_archive_root=None,
    ):
        self.case_dir = case_dir
        self.postgres_engine = postgres_engine
        self.anonymize_files = anonymize_files
        self.delete_originals_after_verification = delete_originals_after_verification
        self.import_id = import_id
        self.import_label = import_label
        # Dataset/cohort tag for this batch (e.g. 'crisp2'). Lives only on the
        # `patient` table (union-accumulated), not on image_study/image_series.
        self.dataset = dataset
        self.cold_archive_root = cold_archive_root

        self.case_series_table = None
        self.case_study_table = None
        self.case_series_verification_table = None

        self.image_study = None
        self.image_series = None
        self.clinical_data = None

        # Canonical destination for loose DICOMs. Read from repo-root config.toml
        # (web-app/config.py DICOM_DATA_ROOT) so the protocol always agrees
        # with the running stack about where files live.
        self.base_dir = str(DICOM_DATA_ROOT)

    def execute_image_integration_protocol(self, overwrite_if_exists=False):
        case_name = os.path.basename(os.path.normpath(self.case_dir))
        protocol_start = time.perf_counter()
        print(f"Starting image integration for case {case_name}")

        step_started = time.perf_counter()
        self.create_series_table()
        print(
            f"Scanned case {case_name}: discovered {len(self.case_series_table)} readable series "
            f"in {time.perf_counter() - step_started:.2f}s"
        )
        if self.case_series_table is None or self.case_series_table.empty:
            print(f"No readable DICOM series found under case_dir ({self.case_dir})")
            return {"studyinstanceuids": [], "seriesinstanceuids": []}

        step_started = time.perf_counter()
        self.create_study_table()
        print(
            f"Grouped case {case_name} into {len(self.case_study_table)} study rows "
            f"in {time.perf_counter() - step_started:.2f}s"
        )
        if self.case_study_table is None or self.case_study_table.empty:
            print(f"No valid studies could be created from case_dir ({self.case_dir})")
            return {"studyinstanceuids": [], "seriesinstanceuids": []}

        initial_study_count = len(self.case_study_table)
        step_started = time.perf_counter()
        self.filter_existing_studies(overwrite_if_exists=overwrite_if_exists)
        print(
            f"Checked existing studies for case {case_name} in "
            f"{time.perf_counter() - step_started:.2f}s"
        )
        if self.case_series_table.empty:
            if initial_study_count == 0:
                print(f"No valid studies could be created from case_dir ({self.case_dir})")
            else:
                print(
                    f"All series for case_dir ({self.case_dir}) are already in the database"
                )
            return {"studyinstanceuids": [], "seriesinstanceuids": []}

        step_started = time.perf_counter()
        self.load_clinical_data_table()
        print(f"Loaded clinical data in {time.perf_counter() - step_started:.2f}s")

        step_started = time.perf_counter()
        self.validate_studies_against_clinical_data()
        print(f"Validated clinical matches in {time.perf_counter() - step_started:.2f}s")

        step_started = time.perf_counter()
        self.assign_import_id()
        # case_study_table is empty in pure append-only runs (only new series
        # under existing studies); fall back to case_series_table.
        assigned_import_id = (
            self.case_study_table["import_id"].iloc[0]
            if not self.case_study_table.empty
            else self.case_series_table["import_id"].iloc[0]
        )
        print(
            f"Assigned import_id={assigned_import_id} "
            f"in {time.perf_counter() - step_started:.2f}s"
        )
        self.assign_import_label()

        step_started = time.perf_counter()
        self.add_paths_and_copy_dicom_files()
        print(f"Copied DICOM files in {time.perf_counter() - step_started:.2f}s")

        if self.cold_archive_root:
            step_started = time.perf_counter()
            self.compress_cold_archives()
            print(f"Compressed cold archives in {time.perf_counter() - step_started:.2f}s")

        if STORAGE_MODE == "cold_path_cache":
            print(
                "Skipping NIFTI generation in cold_path_cache mode "
                "(use scripts/dicom_to_nifti.py to generate on demand)."
            )
            # Initialize an empty nifti_path column so downstream upserts have it.
            if "nifti_path" not in self.case_series_table.columns:
                self.case_series_table["nifti_path"] = ""
        else:
            step_started = time.perf_counter()
            self.create_nifti_files()
            print(f"Generated NIFTI files in {time.perf_counter() - step_started:.2f}s")

        self.case_series_verification_table = self.case_series_table[
            ["copied_pairs", "dicom_dir_path"]
        ].copy()
        self.format_column_names()

        step_started = time.perf_counter()
        self._require_import_id_columns()
        self._require_import_label_columns()
        self._require_number_of_slices_column()
        if self.cold_archive_root:
            self._require_dicom_archive_path_column()
        self.update_postgres_tables()
        print(f"Updated PostgreSQL tables in {time.perf_counter() - step_started:.2f}s")

        if self.delete_originals_after_verification:
            step_started = time.perf_counter()
            self.verify_integrated_case()
            self.delete_original_case_dir()
            print(
                f"Verified integrated files and deleted originals in "
                f"{time.perf_counter() - step_started:.2f}s"
            )
        print(
            f"Completed image integration for case {case_name} in "
            f"{time.perf_counter() - protocol_start:.2f}s"
        )
        # Derive both lists from case_series_table so labelled-table re-sync
        # also covers existing studies that received new series in this run
        # (those study rows were dropped from case_study_table to avoid
        # overwriting their persisted image_study fields).
        return {
            "studyinstanceuids": sorted(
                self.case_series_table["studyinstanceuid"].dropna().astype(str).unique().tolist()
            ),
            "seriesinstanceuids": sorted(
                self.case_series_table["seriesinstanceuid"].dropna().astype(str).unique().tolist()
            ),
        }

    def load_image_tables(self):
        self.image_study = pd.read_sql_table("image_study", self.postgres_engine)
        self.image_series = pd.read_sql_table("image_series", self.postgres_engine)

    def _load_case_rows_from_db(self, study_uids, include_series=False):
        study_uids = [str(study_uid).strip() for study_uid in study_uids if str(study_uid).strip()]
        if not study_uids:
            self.image_study = pd.DataFrame()
            self.image_series = pd.DataFrame()
            return

        metadata = MetaData()
        image_study_table = Table("image_study", metadata, autoload_with=self.postgres_engine)
        study_columns = [
            image_study_table.c[column_name]
            for column_name in ("studyinstanceuid", "patient_id", "study_path")
            if column_name in image_study_table.c
        ]

        with self.postgres_engine.begin() as connection:
            study_rows = connection.execute(
                select(*study_columns).where(image_study_table.c.studyinstanceuid.in_(study_uids))
            ).mappings().all()

            series_rows = []
            if include_series:
                image_series_table = Table(
                    "image_series", metadata, autoload_with=self.postgres_engine
                )
                series_columns = [
                    image_series_table.c[column_name]
                    for column_name in (
                        "studyinstanceuid",
                        "seriesinstanceuid",
                        "dicom_dir_path",
                        "dicom_archive_path",
                        "number_of_slices",
                    )
                    if column_name in image_series_table.c
                ]
                series_rows = connection.execute(
                    select(*series_columns).where(image_series_table.c.studyinstanceuid.in_(study_uids))
                ).mappings().all()

        self.image_study = pd.DataFrame(study_rows, columns=[column.key for column in study_columns])
        self.image_series = pd.DataFrame(
            series_rows,
            columns=[column.key for column in series_columns] if include_series else [],
        )

    def load_clinical_data_table(self):
        self.clinical_data = pd.read_sql_table("lvo_clinical_data", self.postgres_engine)
        if "study_id" in self.clinical_data.columns:
            self.clinical_data["study_id"] = self.clinical_data["study_id"].apply(
                lambda value: str(value).strip() if pd.notna(value) else None
            )
        if "stroke_date" in self.clinical_data.columns:
            self.clinical_data["stroke_date"] = pd.to_datetime(
                self.clinical_data["stroke_date"], errors="coerce"
            ).dt.normalize()

    @staticmethod
    def _empty_series_table():
        return pd.DataFrame(
            columns=[
                "patient_id",
                "acquisitiondatetime",
                "studydescription",
                "seriesdescription",
                "studyinstanceuid",
                "seriesinstanceuid",
                "number_of_slices",
                "src_file_paths",
                "protocolname",
                "seriesnumber",
                "instancenumber",
                "manufacturer",
                "pixelspacing",
                "slicethickness",
                "imageshape",
                "scanaxialcoverage_mm",
                "seriesdescription_",
                "series_type",
                "modality",
                "import_id",
                "import_label",
            ]
        )

    @staticmethod
    def _empty_study_table():
        return pd.DataFrame(
            columns=[
                "patient_id",
                "acquisitiondatetime",
                "study_type",
                "studydescription",
                "studyinstanceuid",
                "study_path",
                "protocolname",
                "manufacturer",
                "predicted_study_type",
                "stroke_date",
                "clinical_match_found",
                "import_id",
                "import_label",
            ]
        )

    @staticmethod
    def _safe_text(value):
        if value is None:
            return None
        if isinstance(value, float) and pd.isna(value):
            return None
        cleaned = str(value).strip()
        return cleaned if cleaned else None

    @staticmethod
    def _safe_int(value):
        if value is None:
            return None
        if isinstance(value, float) and pd.isna(value):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value):
        if value is None:
            return None
        if isinstance(value, float) and pd.isna(value):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _safe_float_array(cls, value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        if not isinstance(value, (list, tuple)):
            value = [value]
        result = [cls._safe_float(item) for item in value]
        return result if any(item is not None for item in result) else None

    @staticmethod
    def _dicom_value(dataset, tag_name):
        if tag_name not in dataset:
            return None
        value = dataset[tag_name].value
        if isinstance(value, bytes):
            value = value.decode(errors="ignore")
        return value

    @classmethod
    def _parse_datetime(cls, dataset):
        candidates = [
            ("AcquisitionDateTime", None),
            ("AcquisitionDate", "AcquisitionTime"),
            ("StudyDate", "StudyTime"),
        ]
        for date_tag, time_tag in candidates:
            if time_tag is None:
                value = cls._safe_text(cls._dicom_value(dataset, date_tag))
                if value:
                    parsed = pd.to_datetime(value.split(".")[0], errors="coerce")
                    if pd.notna(parsed):
                        return parsed
                continue

            date_value = cls._safe_text(cls._dicom_value(dataset, date_tag))
            time_value = cls._safe_text(cls._dicom_value(dataset, time_tag))
            if date_value:
                if time_value is None:
                    time_value = "000000"
                parsed = pd.to_datetime(
                    f"{date_value}{time_value.split('.')[0]}",
                    format="%Y%m%d%H%M%S",
                    errors="coerce",
                )
                if pd.notna(parsed):
                    return parsed
        return pd.NaT

    @classmethod
    def _series_geometry(cls, headers):
        if not headers:
            return None, None, None

        first = headers[0]
        rows = cls._safe_int(cls._dicom_value(first, "Rows"))
        cols = cls._safe_int(cls._dicom_value(first, "Columns"))
        image_shape = [rows, cols, len(headers)] if rows and cols else None

        slice_thickness = cls._safe_float(cls._dicom_value(first, "SliceThickness"))
        spacing_between_slices = cls._safe_float(
            cls._dicom_value(first, "SpacingBetweenSlices")
        )

        z_positions = []
        for dcm in headers:
            position = cls._dicom_value(dcm, "ImagePositionPatient")
            # pydicom returns ImagePositionPatient as a MultiValue, which is NOT
            # a list/tuple — an isinstance((list, tuple)) guard silently rejects
            # it and leaves z_positions empty. Accept any non-string sequence of
            # length >= 3 so the true z-extent is used (not just the fallbacks).
            if (
                position is not None
                and not isinstance(position, (str, bytes))
                and hasattr(position, "__len__")
                and len(position) >= 3
            ):
                z_value = cls._safe_float(position[2])
                if z_value is not None:
                    z_positions.append(z_value)

        scan_axial_coverage_mm = None
        if len(z_positions) > 1 and slice_thickness is not None:
            scan_axial_coverage_mm = (
                max(z_positions) - min(z_positions) + slice_thickness
            )
        elif spacing_between_slices is not None and len(headers) > 1:
            scan_axial_coverage_mm = spacing_between_slices * (len(headers) - 1)
            if slice_thickness is not None:
                scan_axial_coverage_mm += slice_thickness
        elif slice_thickness is not None:
            scan_axial_coverage_mm = slice_thickness * len(headers)

        return image_shape, slice_thickness, scan_axial_coverage_mm

    def create_series_table(self):
        # Group every readable file in the case by its embedded
        # SeriesInstanceUID rather than by directory. A DICOM series is defined
        # by its SeriesInstanceUID, not by where its files happen to live:
        # a "mixed" folder can hold several series, and one series' files can be
        # split across folders. Emitting one row per UID (aggregating every file
        # that carries it) is lossless and guarantees the upsert conflict key is
        # unique within the batch, so the multi-row ON CONFLICT cannot raise a
        # CardinalityViolation (see _upsert_dataframe).
        buckets = {}  # series_uid -> dict(paths, headers, study_uids, series_numbers, dirs)
        for root, _, files in os.walk(self.case_dir):
            for filename in sorted(files):
                if filename.startswith("."):
                    continue
                filepath = os.path.join(root, filename)
                if not os.path.isfile(filepath):
                    continue
                try:
                    dcm = pydicom.dcmread(filepath, stop_before_pixels=True)
                except Exception as exc:
                    print(f"Skipping unreadable file {filepath}: {exc}")
                    continue

                patient_id = self._safe_text(self._dicom_value(dcm, "PatientID"))
                study_instance_uid = self._safe_text(
                    self._dicom_value(dcm, "StudyInstanceUID")
                )
                series_instance_uid = self._safe_text(
                    self._dicom_value(dcm, "SeriesInstanceUID")
                )
                if patient_id is None:
                    print(
                        f"Skipping file without PatientID {filepath}. "
                        "Stanford integration requires PatientID to contain study_id."
                    )
                    continue
                if study_instance_uid is None or series_instance_uid is None:
                    print(f"Skipping file without UIDs {filepath}")
                    continue

                bucket = buckets.get(series_instance_uid)
                if bucket is None:
                    bucket = {
                        "paths": [],
                        "headers": [],
                        "study_uids": set(),
                        "series_numbers": set(),
                        "dirs": set(),
                    }
                    buckets[series_instance_uid] = bucket
                bucket["paths"].append(filepath)
                bucket["headers"].append(dcm)
                bucket["study_uids"].add(study_instance_uid)
                series_number = self._safe_int(self._dicom_value(dcm, "SeriesNumber"))
                if series_number is not None:
                    bucket["series_numbers"].add(series_number)
                bucket["dirs"].add(root)

        data_series_list = []
        for series_instance_uid, bucket in buckets.items():
            # Order instances deterministically (InstanceNumber, then path) so the
            # representative header and the computed geometry are stable run-to-run.
            order = sorted(
                range(len(bucket["paths"])),
                key=lambda i: (
                    self._safe_int(
                        self._dicom_value(bucket["headers"][i], "InstanceNumber")
                    )
                    or 0,
                    bucket["paths"][i],
                ),
            )
            paths = [bucket["paths"][i] for i in order]
            headers = [bucket["headers"][i] for i in order]
            dcm = headers[0]
            patient_id = self._safe_text(self._dicom_value(dcm, "PatientID"))
            study_instance_uid = self._safe_text(
                self._dicom_value(dcm, "StudyInstanceUID")
            )

            # A real series belongs to exactly one study and carries one
            # SeriesNumber. Divergence means the source mis-stamped UIDs (a DICOM
            # standard violation). Per project decision we do NOT split or re-mint
            # UIDs — we keep the files merged as one series and warn loudly so the
            # source can be inspected.
            sorted_dirs = sorted(
                os.path.relpath(d, self.case_dir) for d in bucket["dirs"]
            )
            if len(bucket["study_uids"]) > 1:
                print(
                    f"WARNING: SeriesInstanceUID {series_instance_uid} spans "
                    f"{len(bucket['study_uids'])} StudyInstanceUIDs "
                    f"{sorted(bucket['study_uids'])} across folders {sorted_dirs}; "
                    f"keeping as one series under {study_instance_uid}."
                )
            if len(bucket["series_numbers"]) > 1:
                print(
                    f"WARNING: suspected true SeriesInstanceUID collision for "
                    f"{series_instance_uid}: {len(paths)} files carry differing "
                    f"SeriesNumbers {sorted(bucket['series_numbers'])} across "
                    f"folders {sorted_dirs}. Keeping them merged as one series "
                    f"(no split, no UID re-mint); inspect the source."
                )

            image_shape, slice_thickness, scan_axial_coverage_mm = self._series_geometry(
                headers
            )
            modality = self._safe_text(self._dicom_value(dcm, "Modality"))
            series_description = self._safe_text(
                self._dicom_value(dcm, "SeriesDescription")
            )
            # Geometry-first series-type detection: how many frames share a slice
            # location distinguishes dynamic (CTP/PWI/DWI) from static scans.
            same_position_count = max_same_position_count(headers)

            data_series_list.append(
                pd.DataFrame(
                    [
                        {
                            "patient_id": patient_id,
                            "acquisitiondatetime": self._parse_datetime(dcm),
                            "studydescription": self._safe_text(
                                self._dicom_value(dcm, "StudyDescription")
                            ),
                            "seriesdescription": series_description,
                            "studyinstanceuid": study_instance_uid,
                            "seriesinstanceuid": series_instance_uid,
                            "number_of_slices": len(paths),
                            "src_file_paths": paths,
                            "protocolname": self._safe_text(
                                self._dicom_value(dcm, "ProtocolName")
                            ),
                            "seriesnumber": self._safe_int(
                                self._dicom_value(dcm, "SeriesNumber")
                            ),
                            "instancenumber": self._safe_int(
                                self._dicom_value(dcm, "InstanceNumber")
                            ),
                            "manufacturer": self._safe_text(
                                self._dicom_value(dcm, "Manufacturer")
                            ),
                            "pixelspacing": self._safe_float_array(
                                self._dicom_value(dcm, "PixelSpacing")
                            ),
                            "slicethickness": slice_thickness,
                            "imageshape": image_shape,
                            "scanaxialcoverage_mm": scan_axial_coverage_mm,
                            "seriesdescription_": name_sanity_check(
                                series_description or "UNNAMED_SERIES"
                            ),
                            "series_type": identify_series_type(
                                modality, same_position_count, series_description
                            ),
                            "modality": modality,
                        }
                    ]
                )
            )

        if not data_series_list:
            self.case_series_table = self._empty_series_table()
            self.case_study_table = self._empty_study_table()
            return

        self.case_series_table = pd.concat(data_series_list, ignore_index=True)
        self.case_series_table = self.case_series_table.sort_values(
            by=["patient_id", "acquisitiondatetime", "studyinstanceuid", "seriesinstanceuid"],
            na_position="last",
        ).reset_index(drop=True)

    def create_study_table(self):
        if self.case_series_table is None or self.case_series_table.empty:
            self.case_study_table = self._empty_study_table()
            return

        study_rows = []
        for study_instance_uid, study_series in self.case_series_table.groupby("studyinstanceuid"):
            study_series = study_series.sort_values(
                by=["acquisitiondatetime", "seriesnumber", "instancenumber"],
                na_position="last",
            ).reset_index(drop=True)
            first_row = study_series.iloc[0]
            stroke_date = self._lookup_stroke_date(first_row["patient_id"])
            predicted_study_type = self._predict_study_type(study_series, stroke_date)

            study_rows.append(
                {
                    "patient_id": first_row["patient_id"],
                    "acquisitiondatetime": first_row["acquisitiondatetime"],
                    "study_type": "",
                    "studydescription": first_row["studydescription"],
                    "studyinstanceuid": study_instance_uid,
                    "study_path": "",
                    "protocolname": first_row["protocolname"],
                    "manufacturer": first_row["manufacturer"],
                    "predicted_study_type": predicted_study_type,
                    "stroke_date": stroke_date,
                    "clinical_match_found": False,
                }
            )

        self.case_study_table = pd.DataFrame(study_rows)

        # Study-type detection is intentionally kept for future activation, but the
        # active Stanford pipeline must currently leave `study_type` empty.
        self.case_series_table["study_type"] = ""

    def _lookup_stroke_date(self, patient_id):
        if self.clinical_data is None or self.clinical_data.empty:
            return pd.NaT
        matches = self.clinical_data[self.clinical_data["study_id"] == str(patient_id)]
        if matches.empty or "stroke_date" not in matches.columns:
            return pd.NaT
        return matches["stroke_date"].dropna().iloc[0] if matches["stroke_date"].notna().any() else pd.NaT

    def _predict_study_type(self, study_series, stroke_date):
        predicted_study_type = identify_study_type(study_series)

        # Future hook: if study_type activation is re-enabled, stroke_date should be
        # the clinical anchor for BASELINE / THROMBECTOMY / FOLLOW_UP assignment.
        if pd.isna(stroke_date):
            return predicted_study_type

        acquisition_datetime = study_series["acquisitiondatetime"].dropna()
        if acquisition_datetime.empty:
            return predicted_study_type

        acquisition_date = acquisition_datetime.iloc[0].normalize()
        if acquisition_date < stroke_date - pd.Timedelta(days=1):
            return None
        return predicted_study_type

    def filter_existing_studies(self, overwrite_if_exists=False):
        study_uids = self.case_study_table["studyinstanceuid"].dropna().astype(str).unique().tolist()
        print(
            f"Checking {len(study_uids)} study UID(s) against image_study and image_series"
        )
        # Always load image_series so append mode (overwrite_if_exists=False)
        # can filter at the series level, not just the study level — otherwise
        # new series arriving under a previously-integrated study get dropped.
        self._load_case_rows_from_db(study_uids, include_series=True)

        existing_study_uids = (
            set(self.image_study["studyinstanceuid"].astype(str))
            if self.image_study is not None and not self.image_study.empty
            else set()
        )
        existing_series_uids = (
            set(self.image_series["seriesinstanceuid"].astype(str))
            if self.image_series is not None and not self.image_series.empty
            else set()
        )
        print(
            f"Found {len(existing_study_uids)} existing study UID(s) and "
            f"{len(existing_series_uids)} existing series UID(s) for current case"
        )

        appended_study_uids = []
        for idx, row in self.case_study_table.iterrows():
            study_uid = str(row["studyinstanceuid"])
            if study_uid not in existing_study_uids:
                continue

            if overwrite_if_exists:
                print(
                    f"Study row {idx} already in database — overwriting "
                    f"(StudyInstanceUID: {study_uid})"
                )
                self.overwrite_existing_study(study_uid)
            else:
                print(
                    f"Study row {idx} already in database — appending only new "
                    f"series, leaving existing study row untouched "
                    f"(StudyInstanceUID: {study_uid})"
                )
                appended_study_uids.append(study_uid)

        if appended_study_uids:
            # Drop these study rows from case_study_table so update_postgres_tables
            # does not overwrite the persisted import_id / import_label /
            # study_path / acquisitiondatetime on the existing image_study row.
            self.case_study_table = self.case_study_table[
                ~self.case_study_table["studyinstanceuid"].isin(appended_study_uids)
            ].reset_index(drop=True)

            # Build per-UID lookup of DB state for drift comparison.
            db_info_by_uid = {}
            if self.image_series is not None and not self.image_series.empty:
                for _, db_row in self.image_series.iterrows():
                    uid = str(db_row.get("seriesinstanceuid", "") or "")
                    if uid:
                        db_info_by_uid[uid] = {
                            "number_of_slices": db_row.get("number_of_slices"),
                            "dicom_dir_path": db_row.get("dicom_dir_path"),
                            "dicom_archive_path": db_row.get("dicom_archive_path"),
                        }

            appended_study_uid_set = set(appended_study_uids)
            drop_match_uids = set()
            drop_unverifiable_uids = set()
            drift_records = []  # list of (uid, db_n, disk_n, old_dir, old_archive)
            new_under_existing_count = 0

            for _, row in self.case_series_table.iterrows():
                if str(row["studyinstanceuid"]) not in appended_study_uid_set:
                    continue
                series_uid = str(row["seriesinstanceuid"])
                if series_uid not in existing_series_uids:
                    new_under_existing_count += 1
                    continue
                disk_n = row.get("number_of_slices")
                info = db_info_by_uid.get(series_uid, {})
                db_n = info.get("number_of_slices")
                if db_n is None or (isinstance(db_n, float) and pd.isna(db_n)):
                    drop_unverifiable_uids.add(series_uid)
                    continue
                try:
                    db_n_int = int(db_n)
                    disk_n_int = int(disk_n)
                except (TypeError, ValueError):
                    drop_unverifiable_uids.add(series_uid)
                    continue
                if db_n_int == disk_n_int:
                    drop_match_uids.add(series_uid)
                else:
                    drift_records.append(
                        (
                            series_uid,
                            db_n_int,
                            disk_n_int,
                            info.get("dicom_dir_path"),
                            info.get("dicom_archive_path"),
                        )
                    )

            drop_uids = drop_match_uids | drop_unverifiable_uids
            if drop_uids:
                self.case_series_table = self.case_series_table[
                    ~self.case_series_table["seriesinstanceuid"].astype(str).isin(drop_uids)
                ].reset_index(drop=True)

            for series_uid, db_n, disk_n, old_dir, old_archive in drift_records:
                print(
                    f"Slice-count drift for SeriesInstanceUID {series_uid}: "
                    f"image_series has {db_n} slices, disk has {disk_n}. "
                    f"Wiping old files and re-ingesting."
                )
                # Path safety: only delete under base_dir / cold_archive_root.
                if isinstance(old_dir, str) and old_dir:
                    base_abs = os.path.abspath(self.base_dir)
                    old_dir_abs = os.path.abspath(old_dir)
                    if (
                        old_dir_abs.startswith(base_abs + os.sep)
                        and os.path.isdir(old_dir_abs)
                    ):
                        shutil.rmtree(old_dir_abs, ignore_errors=True)
                        print(f"  removed old DICOM dir {old_dir_abs}")
                if (
                    self.cold_archive_root
                    and isinstance(old_archive, str)
                    and old_archive
                ):
                    cold_abs = os.path.abspath(self.cold_archive_root)
                    old_archive_abs = os.path.abspath(old_archive)
                    if (
                        old_archive_abs.startswith(cold_abs + os.sep)
                        and os.path.isfile(old_archive_abs)
                    ):
                        os.remove(old_archive_abs)
                        print(f"  removed stale archive {old_archive_abs}")

            if drop_unverifiable_uids:
                print(
                    f"WARNING: {len(drop_unverifiable_uids)} already-integrated "
                    f"series have NULL/non-integer number_of_slices in image_series; "
                    f"cannot verify drift. Skipping. To force re-ingest, set "
                    f"overwrite_if_exists: true for these studies."
                )

            print(
                f"Append-only filter: skipped {len(drop_match_uids)} matching + "
                f"{len(drop_unverifiable_uids)} unverifiable series already in "
                f"image_series; re-ingesting {len(drift_records)} drift series; "
                f"appending {new_under_existing_count} brand-new series; "
                f"under {len(appended_study_uids)} existing study row(s)."
            )

        print(
            f"Retained {len(self.case_study_table)} new study row(s) and "
            f"{len(self.case_series_table)} series row(s) after filtering"
        )

    def overwrite_existing_study(self, study_instance_uid):
        if self.image_study is None or self.image_series is None:
            self._load_case_rows_from_db([study_instance_uid], include_series=True)

        study_rows = self.image_study[
            self.image_study["studyinstanceuid"].astype(str) == str(study_instance_uid)
        ]
        series_rows = self.image_series[
            self.image_series["studyinstanceuid"].astype(str) == str(study_instance_uid)
        ]

        paths_to_remove = []
        if "study_path" in study_rows.columns:
            paths_to_remove.extend(
                path for path in study_rows["study_path"].dropna().unique() if path
            )
        if "dicom_dir_path" in series_rows.columns:
            paths_to_remove.extend(
                path for path in series_rows["dicom_dir_path"].dropna().unique() if path
            )

        archives_to_remove = []
        if "dicom_archive_path" in series_rows.columns:
            archives_to_remove.extend(
                archive
                for archive in series_rows["dicom_archive_path"].dropna().unique()
                if archive
            )

        old_series_uids = [
            str(uid)
            for uid in series_rows["seriesinstanceuid"].dropna().astype(str).unique()
            if str(uid)
        ]

        for path in sorted(set(paths_to_remove), key=len, reverse=True):
            if os.path.isdir(path):
                print(f"Removing directory: {path}")
                shutil.rmtree(path, ignore_errors=True)

        if self.cold_archive_root:
            cold_abs = os.path.abspath(self.cold_archive_root)
            for archive in sorted(set(archives_to_remove)):
                archive_abs = os.path.abspath(archive)
                if (
                    archive_abs.startswith(cold_abs + os.sep)
                    and os.path.isfile(archive_abs)
                ):
                    print(f"Removing stale archive: {archive_abs}")
                    os.remove(archive_abs)

        if not study_rows.empty:
            patient_id = self._safe_text(study_rows.iloc[0].get("patient_id"))
            if patient_id is not None:
                self._remove_empty_parent_dirs(
                    os.path.join(self.base_dir, patient_id, str(study_instance_uid))
                )

        # Delete the DB rows so a subsequent series that was previously
        # ingested but is no longer on disk does not survive as an orphan.
        # The new scan's series get re-upserted below; only series still on
        # disk will exist in image_series after the protocol completes.
        db_inspector = inspect(self.postgres_engine)
        with self.postgres_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM image_series WHERE studyinstanceuid = :uid"),
                {"uid": str(study_instance_uid)},
            )
            connection.execute(
                text("DELETE FROM image_study WHERE studyinstanceuid = :uid"),
                {"uid": str(study_instance_uid)},
            )
            # Labelled-table rows are not refreshed for entity IDs missing
            # from the post-batch sync call, so explicitly clean them up here.
            if old_series_uids and db_inspector.has_table("image_series_labelled"):
                connection.execute(
                    text(
                        "DELETE FROM image_series_labelled "
                        "WHERE seriesinstanceuid = ANY(:uids)"
                    ),
                    {"uids": old_series_uids},
                )
            if db_inspector.has_table("image_study_labelled"):
                connection.execute(
                    text(
                        "DELETE FROM image_study_labelled "
                        "WHERE studyinstanceuid = :uid"
                    ),
                    {"uid": str(study_instance_uid)},
                )

        self.image_study = self.image_study[
            self.image_study["studyinstanceuid"].astype(str) != str(study_instance_uid)
        ].reset_index(drop=True)
        self.image_series = self.image_series[
            self.image_series["studyinstanceuid"].astype(str) != str(study_instance_uid)
        ].reset_index(drop=True)

    def _remove_empty_parent_dirs(self, path):
        current_path = os.path.dirname(path)
        base_dir_abs = os.path.abspath(self.base_dir)
        while os.path.abspath(current_path).startswith(base_dir_abs) and current_path != base_dir_abs:
            if not os.path.isdir(current_path):
                current_path = os.path.dirname(current_path)
                continue
            if os.listdir(current_path):
                break
            print(f"Removing empty directory: {current_path}")
            os.rmdir(current_path)
            current_path = os.path.dirname(current_path)

    def validate_studies_against_clinical_data(self):
        if self.clinical_data is None:
            self.load_clinical_data_table()

        if self.case_study_table.empty:
            return

        clinical_study_ids = set(self.clinical_data["study_id"].dropna().astype(str))
        self.case_study_table["clinical_match_found"] = self.case_study_table["patient_id"].astype(str).isin(
            clinical_study_ids
        )

        for idx, row in self.case_study_table.iterrows():
            patient_id = str(row["patient_id"])
            if row["clinical_match_found"]:
                matches = self.clinical_data[self.clinical_data["study_id"] == patient_id]
                if "stroke_date" in matches.columns and matches["stroke_date"].notna().any():
                    self.case_study_table.loc[idx, "stroke_date"] = matches["stroke_date"].dropna().iloc[0]
                continue

            print(
                f"Warning: study_id {patient_id} is not present in lvo_clinical_data. "
                "The study will still be integrated, but remains clinically unmatched."
            )

    @staticmethod
    def _visible_files(directory):
        if not os.path.isdir(directory):
            return []
        return sorted(
            filename
            for filename in os.listdir(directory)
            if not filename.startswith(".") and os.path.isfile(os.path.join(directory, filename))
        )

    @staticmethod
    def _table_max_import_id(dataframe):
        if dataframe is None or dataframe.empty or "import_id" not in dataframe.columns:
            return None
        values = pd.to_numeric(dataframe["import_id"], errors="coerce").dropna()
        if values.empty:
            return None
        return int(values.max())

    def _require_import_id_columns(self):
        missing_tables = []
        db_inspector = inspect(self.postgres_engine)
        image_series_columns = {
            column["name"] for column in db_inspector.get_columns("image_series")
        }
        image_study_columns = {
            column["name"] for column in db_inspector.get_columns("image_study")
        }
        if "import_id" not in image_series_columns:
            missing_tables.append("image_series")
        if "import_id" not in image_study_columns:
            missing_tables.append("image_study")
        if missing_tables:
            raise ValueError(
                "Missing required import_id column in "
                f"{', '.join(missing_tables)}. "
                "Run the import_id rename migration before executing the protocol."
            )

    def _require_import_label_columns(self):
        missing_tables = []
        db_inspector = inspect(self.postgres_engine)
        image_series_columns = {
            column["name"] for column in db_inspector.get_columns("image_series")
        }
        image_study_columns = {
            column["name"] for column in db_inspector.get_columns("image_study")
        }
        if "import_label" not in image_series_columns:
            missing_tables.append("image_series")
        if "import_label" not in image_study_columns:
            missing_tables.append("image_study")
        if missing_tables:
            raise ValueError(
                "Missing required import_label column in "
                f"{', '.join(missing_tables)}. "
                "Run the import_label migration before executing the protocol."
            )

    def _require_number_of_slices_column(self):
        db_inspector = inspect(self.postgres_engine)
        image_series_columns = {
            column["name"] for column in db_inspector.get_columns("image_series")
        }
        if "number_of_slices" not in image_series_columns:
            raise ValueError(
                "Missing required number_of_slices column in image_series. "
                "Run: ALTER TABLE image_series ADD COLUMN IF NOT EXISTS number_of_slices INTEGER;"
            )

    @staticmethod
    def get_next_import_id(postgres_engine):
        db_inspector = inspect(postgres_engine)
        image_series_columns = {
            column["name"] for column in db_inspector.get_columns("image_series")
        }
        image_study_columns = {
            column["name"] for column in db_inspector.get_columns("image_study")
        }
        missing_tables = []
        if "import_id" not in image_series_columns:
            missing_tables.append("image_series")
        if "import_id" not in image_study_columns:
            missing_tables.append("image_study")
        if missing_tables:
            raise ValueError(
                "Missing required import_id column in "
                f"{', '.join(missing_tables)}. "
                "Run the import_id rename migration before executing the protocol."
            )

        metadata = MetaData()
        image_series_table = Table("image_series", metadata, autoload_with=postgres_engine)
        image_study_table = Table("image_study", metadata, autoload_with=postgres_engine)
        with postgres_engine.begin() as connection:
            series_max = connection.execute(
                select(func.max(image_series_table.c.import_id))
            ).scalar()
            study_max = connection.execute(
                select(func.max(image_study_table.c.import_id))
            ).scalar()

        max_import_id = max(
            value for value in [series_max, study_max, -1] if value is not None
        )
        return int(max_import_id) + 1

    def assign_import_id(self):
        self._require_import_id_columns()
        next_import_id = (
            int(self.import_id)
            if self.import_id is not None
            else self.get_next_import_id(self.postgres_engine)
        )
        self.case_series_table["import_id"] = next_import_id
        self.case_study_table["import_id"] = next_import_id

    def assign_import_label(self):
        self._require_import_label_columns()
        self.case_series_table["import_label"] = self.import_label
        self.case_study_table["import_label"] = self.import_label

    def _copy_dicom_file(self, source_path, destination_path, patient_id):
        if self.anonymize_files:
            dcm = pydicom.dcmread(source_path)
            dcm = anonymize_dicom_slice(dcm, study_id=patient_id)
            dcm.save_as(destination_path)
            return
        shutil.copy2(source_path, destination_path)

    def add_paths_and_copy_dicom_files(self):
        self.case_series_table["dicom_dir_path"] = ""
        if "copied_pairs" not in self.case_series_table.columns:
            self.case_series_table["copied_pairs"] = None
        self.case_study_table["study_path"] = ""

        for idx, row in self.case_series_table.iterrows():
            action = "Copying and anonymizing" if self.anonymize_files else "Copying"
            print(
                f"{action} DICOM {row['seriesdescription']} for study_id "
                f"{row['patient_id']} (series {idx + 1} of {len(self.case_series_table)})"
            )

            study_path = os.path.join(
                self.base_dir, str(row["patient_id"]), row["studyinstanceuid"]
            )
            dicom_dir_path = os.path.join(
                study_path,
                row["seriesdescription_"],
                row["seriesinstanceuid"],
                "DICOM",
            )
            os.makedirs(dicom_dir_path, exist_ok=True)

            copied_pairs = []
            used_names = set()
            for source_path in row["src_file_paths"]:
                base_name = os.path.basename(source_path)
                dest_name = base_name
                # A series' files may come from several source folders; guard
                # against basename collisions so no instance is overwritten.
                if dest_name in used_names:
                    stem, ext = os.path.splitext(base_name)
                    suffix = 1
                    while dest_name in used_names:
                        dest_name = f"{stem}__dup{suffix}{ext}"
                        suffix += 1
                used_names.add(dest_name)
                destination_path = os.path.join(dicom_dir_path, dest_name)
                self._copy_dicom_file(source_path, destination_path, row["patient_id"])
                copied_pairs.append((source_path, destination_path))

            self.case_series_table.loc[idx, "dicom_dir_path"] = dicom_dir_path
            self.case_series_table.at[idx, "copied_pairs"] = copied_pairs
            self.case_study_table.loc[
                self.case_study_table["studyinstanceuid"] == row["studyinstanceuid"],
                "study_path",
            ] = study_path

    def verify_integrated_case(self):
        verification_table = self.case_series_verification_table
        if verification_table is None:
            verification_table = self.case_series_table

        if verification_table is None or verification_table.empty:
            return

        for _, row in verification_table.iterrows():
            copied_pairs = row.get("copied_pairs") or []
            dicom_dir_path = row["dicom_dir_path"]
            destination_files = self._visible_files(dicom_dir_path)

            if len(copied_pairs) != len(destination_files):
                raise ValueError(
                    f"Verification failed for {dicom_dir_path}: expected "
                    f"{len(copied_pairs)} files, destination has "
                    f"{len(destination_files)}."
                )

            for source_path, destination_path in copied_pairs:
                filename = os.path.basename(destination_path)
                if not os.path.exists(destination_path):
                    raise ValueError(
                        f"Verification failed for {dicom_dir_path}: missing file {filename}."
                    )

                if not self.anonymize_files:
                    source_size = os.path.getsize(source_path)
                    destination_size = os.path.getsize(destination_path)
                    if source_size != destination_size:
                        raise ValueError(
                            f"Verification failed for {dicom_dir_path}: "
                            f"size mismatch for {filename} ({source_size} != {destination_size})."
                        )

                try:
                    pydicom.dcmread(destination_path, stop_before_pixels=True)
                except Exception as exc:
                    raise ValueError(
                        f"Verification failed for {destination_path}: unreadable DICOM ({exc})."
                    ) from exc

    def delete_original_case_dir(self):
        case_dir_abs = os.path.abspath(self.case_dir)
        base_dir_abs = os.path.abspath(self.base_dir)
        if not os.path.isdir(case_dir_abs):
            return
        if case_dir_abs == base_dir_abs or case_dir_abs.startswith(f"{base_dir_abs}{os.sep}"):
            raise ValueError(
                f"Refusing to delete source directory inside integration base_dir: {case_dir_abs}"
            )
        print(f"Deleting original case directory after verification: {case_dir_abs}")
        shutil.rmtree(case_dir_abs)

    def create_nifti_files(self):
        self.case_series_table["nifti_path"] = ""

        for idx, row in self.case_series_table.iterrows():
            dicom_dir_path = row["dicom_dir_path"]
            nifti_path = os.path.join(
                os.path.dirname(dicom_dir_path), "NIFTI", "image.nii.gz"
            )

            if os.path.exists(dicom_dir_path) and should_create_nifti(row["series_type"]):
                os.makedirs(os.path.dirname(nifti_path), exist_ok=True)
                print(f"Converting {dicom_dir_path} to {nifti_path}")
                try:
                    convert_dicom_to_nifti(dicom_dir_path, nifti_path)
                except Exception as exc:
                    print(f"Error converting {dicom_dir_path} to {nifti_path}: {exc}")

            if os.path.exists(nifti_path):
                self.case_series_table.loc[idx, "nifti_path"] = nifti_path

        return self.case_series_table

    def format_column_names(self):
        if self.case_series_table is None:
            self.case_series_table = self._empty_series_table()
        if self.case_study_table is None:
            self.case_study_table = self._empty_study_table()

        if "dicom_archive_path" not in self.case_series_table.columns:
            self.case_series_table["dicom_archive_path"] = None

        self.case_series_table = self.case_series_table[
            [
                "patient_id",
                "acquisitiondatetime",
                "studydescription",
                "seriesdescription",
                "series_type",
                "modality",
                "studyinstanceuid",
                "seriesinstanceuid",
                "number_of_slices",
                "dicom_dir_path",
                "dicom_archive_path",
                "nifti_path",
                "import_id",
                "import_label",
                "protocolname",
                "seriesnumber",
                "instancenumber",
                "manufacturer",
                "pixelspacing",
                "slicethickness",
                "imageshape",
                "scanaxialcoverage_mm",
            ]
        ].copy()

        self.case_study_table = self.case_study_table[
            [
                "patient_id",
                "acquisitiondatetime",
                "study_type",
                "studydescription",
                "studyinstanceuid",
                "study_path",
                "import_id",
                "import_label",
                "protocolname",
                "manufacturer",
            ]
        ].copy()

    @staticmethod
    def _normalize_for_sql(value):
        if value is None:
            return None
        if isinstance(value, pd.Timestamp):
            return None if pd.isna(value) else value.to_pydatetime()
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, float) and pd.isna(value):
            return None
        if isinstance(value, list):
            return [ImageIntegrationProtocol._normalize_for_sql(item) for item in value]
        return value

    def _upsert_dataframe(self, table_name, key_column, dataframe, connection):
        if dataframe.empty:
            return

        # Belt-and-suspenders: a single multi-row INSERT ... ON CONFLICT cannot
        # touch the same conflict target twice (Postgres CardinalityViolation),
        # which would roll back the whole case. Grouping series by UID upstream
        # already keeps keys unique, but guard here so a stray duplicate can
        # never again silently drop an entire case — keep the last occurrence.
        duplicate_mask = dataframe.duplicated(subset=[key_column], keep="last")
        if duplicate_mask.any():
            dropped = int(duplicate_mask.sum())
            print(
                f"WARNING: {table_name} upsert had {dropped} row(s) with a "
                f"duplicate {key_column}; keeping the last occurrence of each to "
                f"avoid a CardinalityViolation. Inspect the source data."
            )
            dataframe = dataframe[~duplicate_mask]

        metadata = MetaData()
        table = Table(table_name, metadata, autoload_with=self.postgres_engine)
        records = [
            {column: self._normalize_for_sql(value) for column, value in row.items()}
            for row in dataframe.to_dict(orient="records")
        ]

        insert_stmt = pg_insert(table).values(records)
        update_columns = {
            column.name: insert_stmt.excluded[column.name]
            for column in table.columns
            if column.name != key_column
        }
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[key_column],
            set_=update_columns,
        )
        connection.execute(upsert_stmt)

    def _upsert_patient(self, connection):
        """Register/refresh one `patient` row per patient_id in this batch.

        Must run AFTER the image_study upsert so MIN(acquisitiondatetime) sees
        the new studies. stroke_date is recomputed from the DB (all of the
        patient's studies, not just this batch). import_id/import_label keep
        ORIGIN (first-seen) semantics — preserved on conflict; dataset is the
        deduped union across batches; updated_at advances on every touch.

        Patient ids come from case_series_table, not case_study_table: in pure
        append-only runs (new series under an existing study) the study row is
        dropped from case_study_table, but the patient still needs its dataset
        unioned and stroke_date refreshed. case_series_table always carries
        every patient touched this run.
        """
        if self.case_series_table is None or self.case_series_table.empty:
            return
        patient_ids = sorted(
            {str(pid) for pid in self.case_series_table["patient_id"].dropna().unique()}
        )
        if not patient_ids:
            return

        dataset_arr = [self.dataset] if self.dataset else []
        connection.execute(
            text(
                "INSERT INTO patient "
                "(patient_id, stroke_date, import_id, import_label, dataset, "
                " created_at, updated_at) "
                "SELECT s.patient_id, MIN(s.acquisitiondatetime), "
                "       :import_id, :import_label, :dataset, now(), now() "
                "FROM image_study s "
                "WHERE s.patient_id = ANY(:patient_ids) "
                "GROUP BY s.patient_id "
                "ON CONFLICT (patient_id) DO UPDATE SET "
                "  stroke_date = EXCLUDED.stroke_date, "
                "  dataset = ARRAY(SELECT DISTINCT unnest("
                "      patient.dataset || EXCLUDED.dataset) ORDER BY 1), "
                "  updated_at = now()"
            ),
            {
                "import_id": self.import_id,
                "import_label": self.import_label,
                "dataset": dataset_arr,
                "patient_ids": patient_ids,
            },
        )

    def update_postgres_tables(self):
        # One transaction for all three tables: image_study must be committed
        # together with the patient registry, else a crash between them leaves
        # studies with no patient row (the bug this table fixes).
        with self.postgres_engine.begin() as connection:
            self._upsert_dataframe(
                "image_series", "seriesinstanceuid", self.case_series_table, connection
            )
            self._upsert_dataframe(
                "image_study", "studyinstanceuid", self.case_study_table, connection
            )
            self._upsert_patient(connection)

    def _require_dicom_archive_path_column(self):
        """Add dicom_archive_path column to image_series if it does not exist."""
        with self.postgres_engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE image_series ADD COLUMN IF NOT EXISTS dicom_archive_path TEXT"
            ))

    def _compress_series_dir(self, dicom_dir_path: str):
        """Compress a series DICOM directory to a tar.zst archive.

        Returns the archive path (str). Idempotent: skips compression if a
        valid archive already exists, but always re-verifies the archive
        before returning. Raises on any failure (missing source dir,
        empty source, write failure, count mismatch after compression).
        """
        dicom_dir = os.path.realpath(dicom_dir_path)
        base_dir = os.path.realpath(self.base_dir)
        cold_root = os.path.realpath(self.cold_archive_root)

        if not os.path.isdir(dicom_dir):
            raise FileNotFoundError(f"dicom_dir_path does not exist: {dicom_dir_path}")

        # Compute relative path from base_dir.
        rel = os.path.relpath(dicom_dir, base_dir)
        if rel.startswith(".."):
            raise ValueError(
                f"dicom_dir_path {dicom_dir_path!r} is not under base_dir {self.base_dir!r}"
            )

        rel_parent = os.path.dirname(rel)
        series_name = os.path.basename(rel)
        archive = os.path.join(cold_root, rel_parent, f"{series_name}.tar.zst")

        # Source file count for verification.
        src_files = sorted(p for p in Path(dicom_dir).rglob("*") if p.is_file())
        expected_count = len(src_files)
        if expected_count == 0:
            raise ValueError(f"dicom_dir_path is empty: {dicom_dir_path}")

        # Idempotent path: if an archive already exists, verify it matches the
        # source count. If it doesn't, treat it as corrupt and rebuild.
        if os.path.isfile(archive) and os.path.getsize(archive) > 0:
            try:
                self._verify_archive(archive, expected_count)
                return archive
            except Exception as exc:
                print(
                    f"Existing archive {archive} failed verification "
                    f"({exc}); rebuilding."
                )
                os.remove(archive)

        os.makedirs(os.path.dirname(archive), exist_ok=True)

        # Flat format: files stored at archive root (relative to dicom_dir),
        # matching the layout produced by scripts/archive_all_series.py.
        tmp_archive = archive + ".tmp"
        try:
            cctx = zstd.ZstdCompressor(level=3)
            with open(tmp_archive, "wb") as f_out:
                with cctx.stream_writer(f_out) as z_out:
                    with tarfile.open(fileobj=z_out, mode="w|") as tf:
                        for f in src_files:
                            tf.add(str(f), arcname=str(f.relative_to(dicom_dir)))

            # Verify before publishing the final filename.
            self._verify_archive(tmp_archive, expected_count)
            os.replace(tmp_archive, archive)
        except Exception:
            # Don't leave a partial .tmp file behind.
            if os.path.exists(tmp_archive):
                try:
                    os.remove(tmp_archive)
                except OSError:
                    pass
            raise

        return archive

    @staticmethod
    def _verify_archive(archive_path: str, expected_count: int) -> None:
        """Open the tar.zst archive and confirm it contains exactly
        `expected_count` regular files. Raises on any mismatch."""
        if not os.path.isfile(archive_path):
            raise FileNotFoundError(f"archive missing: {archive_path}")
        if os.path.getsize(archive_path) == 0:
            raise ValueError(f"archive is empty: {archive_path}")

        dctx = zstd.ZstdDecompressor()
        actual = 0
        with open(archive_path, "rb") as f_in:
            with dctx.stream_reader(f_in) as z_in:
                with tarfile.open(fileobj=z_in, mode="r|") as tf:
                    for member in tf:
                        if member.isfile():
                            actual += 1
        if actual != expected_count:
            raise ValueError(
                f"archive {archive_path} has {actual} files; expected {expected_count}"
            )

    def compress_cold_archives(self):
        """Compress each series DICOM directory to a cold tar.zst archive.

        Called after add_paths_and_copy_dicom_files() when cold_archive_root
        is set. Per-series failures are non-fatal: the loop continues, the
        failed row keeps `dicom_archive_path = None`, and a JSON failure
        report is written to `image_integration_protocols/logs/`. Each
        successful archive is verified (file count match) before being
        published, courtesy of `_compress_series_dir`.

        Loose DICOM files are NOT deleted — they remain for the Orthanc
        Folder Indexer. See `scripts/cleanup_loose_dicoms.py` for the
        opt-in cleanup pass once the new files have been indexed. Failed
        series are skipped by cleanup (it requires
        `dicom_archive_path IS NOT NULL`).
        """
        if not self.cold_archive_root:
            return

        if "dicom_archive_path" not in self.case_series_table.columns:
            self.case_series_table["dicom_archive_path"] = None

        failures = []  # list[dict]
        successes = 0
        for idx, row in self.case_series_table.iterrows():
            dicom_dir_path = row.get("dicom_dir_path")
            if not dicom_dir_path:
                failures.append(
                    {
                        "row_index": int(idx),
                        "seriesinstanceuid": row.get("seriesinstanceuid"),
                        "studyinstanceuid": row.get("studyinstanceuid"),
                        "dicom_dir_path": None,
                        "error": "missing dicom_dir_path",
                    }
                )
                continue
            try:
                archive = self._compress_series_dir(dicom_dir_path)
                self.case_series_table.loc[idx, "dicom_archive_path"] = str(archive)
                successes += 1
                print(f"Compressed {dicom_dir_path} -> {archive}")
            except Exception as exc:
                failures.append(
                    {
                        "row_index": int(idx),
                        "seriesinstanceuid": row.get("seriesinstanceuid"),
                        "studyinstanceuid": row.get("studyinstanceuid"),
                        "dicom_dir_path": dicom_dir_path,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"WARNING: compression failed for {dicom_dir_path}: {exc}")

        if failures:
            log_path = self._write_compression_failure_log(failures)
            total = successes + len(failures)
            print(
                f"WARNING: {len(failures)}/{total} series failed to compress for "
                f"case {os.path.basename(os.path.normpath(self.case_dir))}. "
                f"Failed rows kept dicom_archive_path = NULL. "
                f"See {log_path}. Retry with: "
                f"`python scripts/archive_all_series.py --patient <patient_id>`"
            )

    def _write_compression_failure_log(self, failures: list[dict]) -> str:
        """Write a JSON failure report and return its path."""
        logs_dir = Path(__file__).resolve().parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = logs_dir / f"compression_failures_{ts}.json"
        payload = {
            "case_dir": str(self.case_dir),
            "case_name": os.path.basename(os.path.normpath(self.case_dir)),
            "import_id": self.import_id,
            "import_label": self.import_label,
            "cold_archive_root": str(self.cold_archive_root) if self.cold_archive_root else None,
            "timestamp": datetime.now().isoformat(),
            "failures": failures,
        }
        with log_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        return str(log_path)