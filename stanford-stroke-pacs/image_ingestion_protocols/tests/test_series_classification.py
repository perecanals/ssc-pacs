"""Series/study classification rules.

Every case below is a real SeriesDescription + ConvolutionKernel pair taken from
the live corpus, not an invented one — these lexicons are only worth what they
score against actual Stanford data.
"""

from datetime import datetime

import pytest

from series_classification import (
    RULES_VERSION,
    assign_episodes,
    assign_patient_timepoints,
    classify_series,
    classify_study,
    classify_timepoint,
    construct_acquisition_datetime,
    resolve_event_anchor,
)


def _tags(modality="CT", descr="", kernel=None, image_type=None, study=None, **extra):
    tags = {"Modality": modality, "SeriesDescription": descr}
    if kernel is not None:
        tags["ConvolutionKernel"] = kernel
    if image_type is not None:
        tags["ImageType"] = image_type
    if study is not None:
        tags["StudyDescription"] = study
    tags.update(extra)
    return tags


# Series-family identity includes diagnostic reconstructions, regardless of plane.
# These are development-cohort descriptions; no held-out examples enter tests.


@pytest.mark.parametrize(
    "modality,description,extra,count,expected",
    [
        (
            "CT",
            "DE_Head 3.0 MPR Ax F_0.5",
            {"ImageType": ["DERIVED", "PRIMARY", "LOCALIZER"]},
            1,
            "NCCT",
        ),
        ("CT", "DE_Head WO 0.75 Hr32 F_0.5", {}, 1, "NCCT"),
        ("CT", "NCCT SRC Head 3.0", {}, 1, "NCCT"),
        (
            "CT",
            "2MM AXIAL HEAD WO CON",
            {"ImageType": ["DERIVED", "SECONDARY", "REFORMATTED"]},
            1,
            "NCCT",
        ),
        (
            "CT",
            "CTA 2.0 MPR Cor",
            {"ImageOrientationPatient": [1, 0, 0, 0, 0, -1]},
            1,
            "CTA",
        ),
        ("CT", "Head/Neck Angio 5.0mm", {}, 1, "CTA"),
        (
            "CT",
            "VOLUME Head 0.5 CE",
            {
                "ConvolutionKernel": "FC68",
                "SliceThickness": 0.5,
                "StudyDescription": "CT HEAD NECK ANGIOGRAPHY",
            },
            1,
            "CTA",
        ),
        ("CT", "CTA, iDose (1)", {"ConvolutionKernel": "UC"}, 1, "CTA"),
        (
            "CT",
            "Cow/Carotid Soft Tissue",
            {"ConvolutionKernel": "H31f", "ContrastBolusAgent": "APPLIED"},
            1,
            "CTA",
        ),
        (
            "CT",
            "Head W/ 3x3",
            {"SliceThickness": 3, "StudyDescription": "CTA HEAD NECK"},
            1,
            "CECT",
        ),
        ("CT", "PERFUSION Head 8.0 CE", {}, 30, "CTP"),
        ("CT", "Top Slab Head 8.0 CE", {}, 1, "CTP"),
        ("MR", "AX PWI", {}, 30, "PWI"),
        ("MR", "Ax EPI-GRE +C", {}, 60, "PWI"),
        ("MR", "Ax DWI (SEND TO RAPID-3)", {}, 4, "DWI"),
        ("MR", "DIFFUSION_ADC", {"DiffusionBValue": 1000}, 4, "ADC"),
        (
            "MR",
            "AXIAL DWI",
            {"ImageType": ["DERIVED", "PRIMARY", "DIFFUSION", "ADC"]},
            1,
            "ADC",
        ),
        ("MR", "Ax GRE", {}, 1, "GRE"),
        ("MR", "AXEPIfastmix-T2 FLAIR", {}, 4, "FLAIR"),
        ("MR", "3DEPI SWI-AX Neuromix", {}, 6, "SWI"),
        ("MR", "Brain Ax 3D MRA_TOF", {}, 1, "MRA_TOF"),
        ("MR", "MRA CAROTID+C", {}, 1, "MRA_CE"),
        (
            "MR",
            "PJN:3DTOF COW MRA fast",
            {"ImageType": ["DERIVED", "PRIMARY", "PROJECTION IMAGE"]},
            1,
            "MRA_TOF",
        ),
        ("MR", "AX 3D ASL (color)", {}, 1, "ASL_SOURCE"),
        ("MR", "(CBF) AX 3D ASL (color)", {}, 1, "ASL_CBF"),
        ("MR", "(Transit delay) eASL: TT map", {}, 1, "ASL_DELAY"),
        ("MR", "(per del, mean PW, REF) eASL: TT map", {}, 1, "ASL_OTHER"),
        ("CT", "RAPID CT-P Summary", {}, 30, "RAPID_SUMMARY"),
        ("MR", "RAPID DWI-PWI Summary", {"DiffusionBValue": 1000}, 6, "RAPID_SUMMARY"),
        ("CT", "PACS nomination form", {}, 1, "DOCUMENT"),
        ("XA", "LICA", {"ImageType": ["DERIVED", "SECONDARY"]}, 1, "DSA"),
        ("CT", "ROUTINE HEAD BONE", {"ConvolutionKernel": "BONEPLUS"}, 1, "CT_BONE"),
        ("CT", "Iodine Map MPR tra 3mm", {}, 1, "CT_MATERIAL"),
    ],
)
def test_development_families(modality, description, extra, count, expected):
    result, rule = classify_series(
        _tags(modality=modality, descr=description, **extra), count, 100
    )
    assert result == expected, rule


@pytest.mark.parametrize(
    "modality,description",
    [
        ("CT", "SCOUT"),
        ("CT", "SN Topo 1.0 Tr20"),
        ("MR", "3Plane Loc SSFSE"),
        ("CT", "3D Lab: Ax batch 20 x 5 MIP"),
        ("CT", "test bolus"),
        ("CT", "Dose Report"),
        ("MR", "unknown"),
        ("OT", "unknown"),
        ("CT", "unknown"),
    ],
)
def test_unknown_or_excluded_series_abstain(modality, description):
    assert (
        classify_series(_tags(modality=modality, descr=description), 1, 100)[0] is None
    )


def test_series_noncontrast_outranks_reused_injection_metadata():
    tags = _tags(
        descr="HEAD WO", ContrastBolusAgent="APPLIED", study="CT HEAD W AND WO CONTRAST"
    )
    assert classify_series(tags, 1, 50)[0] == "NCCT"


def test_mixed_phase_study_is_not_evidence_of_noncontrast():
    from series_detection import contrast_state

    assert contrast_state("ct head w and wo iv contrast") is None
    assert contrast_state("ct head wo iv contrast") is False
    assert contrast_state("ct head w iv contrast") is True


def test_family_does_not_use_patient_identifiers_or_annotations():
    tags = _tags(descr="CTA", PatientID="training-case", label_series_type="NCCT")
    expected = classify_series(tags, 1, 300)
    tags.update(
        PatientID="unseen-case", label_series_type="DWI", InstitutionName="elsewhere"
    )
    assert classify_series(tags, 1, 300) == expected


def test_short_named_cta_is_not_rejected_by_a_vendor_specific_slice_threshold():
    assert classify_series(_tags(descr="CTA HEAD NECK"), 1, 40)[0] == "CTA"


# --- Study level --------------------------------------------------------------


@pytest.mark.parametrize("descr,expected", [
    ("CT HEAD WO IV CONTRAST", "CT_HEAD"),
    ("NIR CEREBRAL ANGIOGRAPHY WITH THROMBECTOMY", "THROMBECTOMY"),
    ("MR BRAIN W AND WO IV CONTRAST STROKE", "MR_BRAIN"),
    ("CTA HEAD NECK W CONTRAST", "CTA"),
    ("CT NECK  REFERENCE ONLY", None),
    ("", None),
])
def test_classify_study(descr, expected):
    stype, _ = classify_study(descr)
    assert stype == expected


def test_stroke_protocol_beats_the_cta_rule():
    # This study names BOTH perfusion and angiography; perfusion must win, or a
    # full stroke protocol is mislabelled as a plain CTA.
    stype, _ = classify_study(
        "CT HEAD W AND WO IV CONTRAST BRAIN PERFUSION CT HEAD NECK ANGIOGRAPHY W IV CONTRAST STROKE"
    )
    assert stype == "CT_STROKE_PROTOCOL"


def test_study_type_does_not_depend_on_series_types():
    # The two axes must stay independent: a series-rule change may never silently
    # move a study's type.
    a, _ = classify_study("CT HEAD WO IV CONTRAST", series_types=["CTA", "CTP"])
    b, _ = classify_study("CT HEAD WO IV CONTRAST", series_types=None)
    assert a == b == "CT_HEAD"


def test_rules_version_is_set():
    assert RULES_VERSION


# --- Timepoint: BL / THROMBECTOMY / FU ----------------------------------------


PUNCTURE = "2020-05-01 12:00:00"


def test_anchor_precedence_prefers_the_recorded_puncture():
    # femoral_sheath_time wins outright — no offset applied.
    anchor, source = resolve_event_anchor({
        "femoral_sheath_time": PUNCTURE,
        "receiving_arrival_time": "2020-05-01 06:00:00",
        "time_recognized": "2020-05-01 01:00:00",
    })
    assert source == "femoral_sheath_time"
    assert anchor == datetime(2020, 5, 1, 12, 0)


def test_arrival_fallback_adds_five_hours():
    anchor, source = resolve_event_anchor({
        "femoral_sheath_time": None,
        "receiving_arrival_time": "2020-05-01 06:00:00",
        "time_recognized": "2020-05-01 01:00:00",
    })
    assert source == "receiving_arrival_time"
    assert anchor == datetime(2020, 5, 1, 11, 0)  # 06:00 + 5h


def test_recognized_fallback_adds_ten_hours():
    anchor, source = resolve_event_anchor({"time_recognized": "2020-05-01 01:00:00"})
    assert source == "time_recognized"
    assert anchor == datetime(2020, 5, 1, 11, 0)  # 01:00 + 10h


def test_no_anchor_yields_null_not_a_guess():
    # 26% of our patients have no anchor column populated at all.
    anchor, source = resolve_event_anchor({"femoral_sheath_time": "", "time_recognized": None})
    assert anchor is None and source is None

    timepoint, hours, rule = classify_timepoint("2020-05-01 08:00:00", None, "CT_HEAD")
    assert timepoint is None
    assert hours is None
    assert rule == "no-clinical-anchor"


@pytest.mark.parametrize("acq,expected,expected_hours", [
    ("2020-05-01 09:00:00", "BL", -3.0),   # 3h before puncture
    ("2020-05-01 12:00:00", "FU", 0.0),    # at the puncture -> at-or-after
    ("2020-05-02 12:00:00", "FU", 24.0),   # next day
])
def test_bl_fu_split_on_the_anchor(acq, expected, expected_hours):
    anchor, _ = resolve_event_anchor({"femoral_sheath_time": PUNCTURE})
    timepoint, hours, _ = classify_timepoint(acq, anchor, "CT_HEAD")
    assert timepoint == expected
    assert hours == expected_hours


def test_hours_to_event_is_signed():
    # A bare BL/FU flag cannot select "the follow-up scan nearest 24h"; the signed
    # offset is what makes that possible.
    anchor, _ = resolve_event_anchor({"femoral_sheath_time": PUNCTURE})
    _, before, _ = classify_timepoint("2020-05-01 06:00:00", anchor, "CT_HEAD")
    _, after, _ = classify_timepoint("2020-05-01 18:00:00", anchor, "CT_HEAD")
    assert before == -6.0
    assert after == 6.0


def test_thrombectomy_study_is_labelled_even_without_an_anchor():
    # The procedure study identifies itself, so it survives a missing anchor.
    timepoint, hours, rule = classify_timepoint("2020-05-01 13:00:00", None, "THROMBECTOMY")
    assert timepoint == "THROMBECTOMY"
    assert hours is None
    assert rule == "study-type-thrombectomy"


def test_thrombectomy_beats_the_temporal_split():
    # Acquired after the anchor, but it IS the procedure — must not read as FU.
    anchor, _ = resolve_event_anchor({"femoral_sheath_time": PUNCTURE})
    timepoint, hours, _ = classify_timepoint("2020-05-01 13:00:00", anchor, "THROMBECTOMY")
    assert timepoint == "THROMBECTOMY"
    assert hours == 1.0


def test_missing_acquisition_time_yields_null():
    anchor, _ = resolve_event_anchor({"femoral_sheath_time": PUNCTURE})
    timepoint, _, rule = classify_timepoint(None, anchor, "CT_HEAD")
    assert timepoint is None
    assert rule == "no-acquisition-time"


def test_stroke_date_is_not_the_anchor():
    # The anchor is puncture, not onset. A row carrying only a stroke/onset date
    # must NOT be treated as an anchor — otherwise BL/FU silently means something
    # different from what the reference implementation means.
    anchor, source = resolve_event_anchor({"stroke_date": "2020-05-01 00:00:00"})
    assert anchor is None and source is None


# --- Acquisition-datetime construction ----------------------------------------


def test_acquisition_datetime_wins_outright():
    dt, source = construct_acquisition_datetime({
        "AcquisitionDateTime": "20200501120000.500000+0100",
        "StudyDate": "20200430", "StudyTime": "000000",
    })
    # Fraction and timezone dropped; acquisition clock preferred.
    assert dt == datetime(2020, 5, 1, 12, 0, 0)
    assert source == "acquisition"


def test_acquisition_date_time_pair_when_no_combined_tag():
    dt, source = construct_acquisition_datetime({
        "AcquisitionDate": "20200501", "AcquisitionTime": "093015",
        "StudyDate": "20200430", "StudyTime": "120000",
    })
    assert dt == datetime(2020, 5, 1, 9, 30, 15)
    assert source == "acquisition"


def test_content_and_series_are_ignored():
    # Derived series (RAPID/MIP/MPR) carry a ContentDate/SeriesDate of when the
    # derivative was COMPUTED — often months after the scan. Probing them would
    # mis-date those series and fabricate spurious episodes, so with no acquisition
    # tag we go straight to the study encounter day, ignoring content/series.
    dt, source = construct_acquisition_datetime({
        "ContentDate": "20250110", "ContentTime": "1030",
        "SeriesDate": "20250110", "SeriesTime": "0900",
        "StudyDate": "20240930", "StudyTime": "025921",
    })
    assert dt == datetime(2024, 9, 30, 2, 59, 21)  # the scan day, not the RAPID day
    assert source == "study"


def test_study_is_the_final_fallback():
    dt, source = construct_acquisition_datetime({"StudyDate": "20200501", "StudyTime": "080000"})
    assert dt == datetime(2020, 5, 1, 8, 0, 0)
    assert source == "study"


def test_no_parseable_datetime():
    assert construct_acquisition_datetime({}) == (None, None)
    assert construct_acquisition_datetime({"StudyDate": ""}) == (None, None)


def test_bare_date_defaults_to_midnight():
    dt, source = construct_acquisition_datetime({"StudyDate": "20200501"})
    assert dt == datetime(2020, 5, 1, 0, 0, 0)
    assert source == "study"


# --- Episode detection --------------------------------------------------------


def test_single_episode_when_studies_are_close():
    dts = ["2020-05-01 08:00:00", "2020-05-01 14:00:00", "2020-05-03 09:00:00"]
    assert assign_episodes(dts) == [1, 1, 1]


def test_large_gap_starts_a_new_episode():
    # The 11-* pattern: two stroke episodes ~14 months apart.
    dts = ["2022-02-19 04:00:00", "2022-02-22 09:00:00", "2023-04-27 16:00:00"]
    assert assign_episodes(dts) == [1, 1, 2]


def test_episode_indices_follow_input_order_not_time_order():
    dts = ["2023-04-27 16:00:00", "2022-02-19 04:00:00"]  # later study first
    assert assign_episodes(dts) == [2, 1]


def test_unparseable_datetime_gets_no_episode():
    assert assign_episodes(["2020-05-01 08:00:00", None, "garbage"]) == [1, None, None]


# --- Episode-aware per-patient timepoints -------------------------------------


def _study(suid, dt, study_type=None):
    return {"studyinstanceuid": suid, "acquisition_datetime": dt, "study_type": study_type}


def test_two_episodes_are_anchored_independently():
    # 11-004-style: a 2022 episode with its own thrombectomy, and a 2023 episode
    # whose femoral-sheath puncture is the only clinical anchor. The old logic
    # scored the 2022 studies against the 2023 anchor (hours_to_event ~ -10000).
    studies = [
        _study("s2022_bl", "2022-02-19 04:00:00", "CT_STROKE_PROTOCOL"),
        _study("s2022_evt", "2022-02-19 06:00:00", "THROMBECTOMY"),
        _study("s2022_fu", "2022-02-20 08:00:00", "CT_HEAD"),
        _study("s2023_bl", "2023-04-27 16:00:00", "CT_HEAD"),
        _study("s2023_fu", "2023-04-27 21:00:00", "MR_BRAIN"),
    ]
    clinical = {"femoral_sheath_time": "2023-04-27 19:10:00"}
    result = assign_patient_timepoints(studies, clinical)

    # Episode 1 (2022) anchors on its own thrombectomy study.
    assert result["s2022_bl"]["episode"] == 1
    assert result["s2022_bl"]["timepoint"] == "BL"
    assert result["s2022_bl"]["timepoint_anchor_source"] == "thrombectomy_study"
    assert result["s2022_evt"]["timepoint"] == "THROMBECTOMY"
    assert result["s2022_fu"]["timepoint"] == "FU"
    # Sanity: hours_to_event is small (own episode), not tens of thousands.
    assert abs(result["s2022_bl"]["hours_to_event"]) < 48

    # Episode 2 (2023) anchors on the clinical puncture.
    assert result["s2023_bl"]["episode"] == 2
    assert result["s2023_bl"]["timepoint"] == "BL"
    assert result["s2023_bl"]["timepoint_anchor_source"] == "femoral_sheath_time"
    assert result["s2023_fu"]["timepoint"] == "FU"


def test_single_episode_lvo_is_unchanged_behaviour():
    # A plain single-episode LVO patient must behave exactly as before rules-v3.
    studies = [
        _study("bl", "2020-05-01 09:00:00", "CT_HEAD"),
        _study("evt", "2020-05-01 12:30:00", "THROMBECTOMY"),
        _study("fu", "2020-05-02 12:00:00", "CT_HEAD"),
    ]
    result = assign_patient_timepoints(studies, {"femoral_sheath_time": PUNCTURE})
    assert {s: result[s]["episode"] for s in result} == {"bl": 1, "evt": 1, "fu": 1}
    assert result["bl"]["timepoint"] == "BL"
    assert result["bl"]["timepoint_anchor_source"] == "femoral_sheath_time"
    assert result["bl"]["hours_to_event"] == -3.0
    assert result["evt"]["timepoint"] == "THROMBECTOMY"
    assert result["fu"]["timepoint"] == "FU"


def test_non_lvo_patient_anchored_on_thrombectomy():
    # No clinical row at all: fall back to the episode's thrombectomy study.
    studies = [
        _study("bl", "2021-03-10 08:00:00", "CT_STROKE_PROTOCOL"),
        _study("evt", "2021-03-10 10:00:00", "THROMBECTOMY"),
        _study("fu", "2021-03-11 08:00:00", "CT_HEAD"),
    ]
    result = assign_patient_timepoints(studies, None)
    assert result["bl"]["timepoint"] == "BL"
    assert result["bl"]["timepoint_anchor_source"] == "thrombectomy_study"
    assert result["evt"]["timepoint"] == "THROMBECTOMY"
    assert result["fu"]["timepoint"] == "FU"


def test_episode_without_any_anchor_is_null():
    # A single episode, no clinical row, no thrombectomy study -> NULL, no guess.
    studies = [
        _study("a", "2021-03-10 08:00:00", "CT_HEAD"),
        _study("b", "2021-03-11 08:00:00", "MR_BRAIN"),
    ]
    result = assign_patient_timepoints(studies, None)
    assert result["a"]["timepoint"] is None
    assert result["a"]["timepoint_anchor_source"] is None
    assert result["a"]["episode"] == 1  # still placed in an episode


def test_study_without_acquisition_time_is_unplaced():
    studies = [_study("a", None, "CT_HEAD")]
    result = assign_patient_timepoints(studies, {"femoral_sheath_time": PUNCTURE})
    assert result["a"]["episode"] is None
    assert result["a"]["timepoint"] is None
    assert result["a"]["timepoint_rule"] == "no-acquisition-time"


def _context_row(uid, description, time="120000", study="study", **tags):
    return dict(
        seriesinstanceuid=uid,
        studyinstanceuid=study,
        tags=_tags(descr=description, AcquisitionTime=time, SliceThickness=1, **tags),
        same_position_count=1,
        n_instances_scanned=120,
    )


def test_repeated_matching_volumes_are_ctp_even_with_separate_series_uids():
    from series_classification import classify_series_batch

    rows = [_context_row(str(i), "Head 1.0 CE", time=f"1200{i:02d}") for i in range(20)]
    result = classify_series_batch(rows)
    assert all(
        value == ("CTP", "ct-temporal-volumes-across-series")
        for value in result.values()
    )


@pytest.mark.parametrize("change", ["study", "time", "same_clock", "missing_clock"])
def test_unrelated_or_reconstructed_volumes_do_not_become_perfusion(change):
    from series_classification import classify_series_batch

    rows = [_context_row(str(i), "Head 1.0 CE", time=f"1200{i:02d}") for i in range(20)]
    for i, row in enumerate(rows):
        if change == "study":
            row["studyinstanceuid"] = f"study-{i}"
        elif change == "time":
            row["tags"]["AcquisitionTime"] = f"12{i:02d}00"
        elif change == "same_clock":
            row["tags"]["AcquisitionTime"] = "120000"
        else:
            row["tags"].pop("AcquisitionTime")
    assert all(value[0] != "CTP" for value in classify_series_batch(rows).values())


def test_named_sibling_resolves_an_ambiguous_reconstruction():
    from series_classification import classify_series_batch

    rows = [
        _context_row("named", "CTA HEAD NECK"),
        _context_row("generic", "Head 1.0", ContrastBolusAgent=""),
    ]
    assert classify_series_batch(rows)["generic"] == (
        "CTA",
        "ct-named-sibling-same-acquisition",
    )


@pytest.mark.parametrize(
    "study,time", [("other-study", "120000"), ("study", "120001"), ("study", "")]
)
def test_sibling_inference_requires_same_study_and_exact_clock(study, time):
    from series_classification import classify_series_batch

    rows = [
        _context_row("named", "CTA HEAD NECK"),
        _context_row("generic", "Head 1.0", study=study, time=time),
    ]
    assert classify_series_batch(rows)["generic"][0] == "NCCT"


def test_conflicting_names_and_explicit_noncontrast_are_not_overwritten():
    from series_classification import classify_series_batch

    rows = [
        _context_row("cta", "CTA HEAD NECK"),
        _context_row("ncct", "NCCT HEAD"),
        _context_row("generic", "Head 1.0"),
    ]
    result = classify_series_batch(rows)
    assert result["cta"][0] == "CTA"
    assert result["ncct"][0] == "NCCT"
    assert result["generic"][0] == "NCCT"


def test_asl_nondiagnostic_marker_can_include_for():
    assert (
        classify_series(
            _tags(modality="MR", descr="NOT FOR DIAGNOSTIC USE: CBF"), 1, 50
        )[0]
        == "ASL_OTHER"
    )


def test_sibling_clock_does_not_override_an_explicit_contrast_phase():
    from series_classification import classify_series_batch
    rows = [_context_row("native", "NCCT HEAD"), _context_row("enhanced", "Head W/ 3x3")]
    rows[1]["tags"]["SliceThickness"] = 3
    assert classify_series_batch(rows)["enhanced"][0] == "CECT"
