"""Series/study classification rules.

Every case below is a real SeriesDescription + ConvolutionKernel pair taken from
the live corpus, not an invented one — these lexicons are only worth what they
score against actual Stanford data.
"""

from datetime import datetime

import pytest

from series_classification import (
    RULES_VERSION,
    classify_series,
    classify_study,
    classify_timepoint,
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


# --- The colleague's central judgement: bone windows are not NCCT -------------


@pytest.mark.parametrize("descr,kernel", [
    ("0.625MM ROUTINE HEAD BONE WO", "BONEPLUS"),
    ("ROUTINE HEAD BONE", "BONEPLUS"),
    ("DE_Head  3.0  H60s  F_0.5", ["H60s"]),  # bone kernel inside a DE study
])
def test_bone_windows_never_become_ncct(descr, kernel):
    # His central judgement: bone kernels are matched BEFORE NCCT kernels. Bone is
    # an exclusion in his scheme, not a type, so it yields None with a rule.
    stype, rule = classify_series(_tags(descr=descr, kernel=kernel), 1)
    assert stype is None
    assert rule == "kernel-bone"


def test_bone_is_matched_before_ncct():
    # "ROUTINE HEAD" hits the NCCT description lexicon; the bone kernel must win.
    stype, _ = classify_series(_tags(descr="ROUTINE HEAD BONE", kernel="BONEPLUS"), 1)
    assert stype != "NCCT"


# --- Geometry (dynamic families) ---------------------------------------------


@pytest.mark.parametrize("modality,count,expected", [
    ("CT", 30, "CTP"),
    ("MR", 30, "PWI"),
    ("MR", 6, "DWI"),
    ("CT", 1, None),
])
def test_geometry_stage(modality, count, expected):
    stype, _ = classify_series(_tags(modality=modality, descr="AX"), count)
    assert stype == expected


def test_mr_can_never_be_ctp():
    # 67 MR series in the corpus carry a CTP label from a pre-modality-guard bug.
    stype, _ = classify_series(_tags(modality="MR", descr="PERFUSION"), 40)
    assert stype != "CTP"


# --- Derived / localizer (new stage; DicomDetector has no equivalent) ---------


@pytest.mark.parametrize("descr,image_type", [
    ("RAPID Perfusion Parameter Maps Colored", ["DERIVED", "SECONDARY", "OTHER"]),
    ("3D Lab:  Ax batch 20 x 5 MIP", ["DERIVED", "SECONDARY", "AQNETSC"]),
    ("RAPID CT-P Summary", ["DERIVED", "SECONDARY", "OTHER"]),
])
def test_derived_products_are_flagged(descr, image_type):
    stype, rule = classify_series(_tags(descr=descr, image_type=image_type), 1)
    assert stype is None
    assert rule in {"description-derived", "imagetype-derived-secondary"}


@pytest.mark.parametrize("descr,image_type", [
    ("SN Topo   1.0  Tr20", ["ORIGINAL", "PRIMARY", "LOCALIZER"]),
    ("SCOUT", ["ORIGINAL", "PRIMARY", "LOCALIZER"]),
    ("3Plane Loc SSFSE", None),
])
def test_localizers_are_flagged(descr, image_type):
    stype, rule = classify_series(_tags(descr=descr, image_type=image_type), 1)
    assert stype is None
    assert rule == "imagetype-localizer"


def test_trace_dwi_survives_the_derived_filter():
    # Scanners stamp trace/isotropic DWI as DERIVED\SECONDARY because it IS
    # computed from the raw directions — but it is the image a rater reads. The
    # carried b-value outranks ImageType. 12 genuine DWI/ADC series in the live
    # corpus were being lost as DERIVED before this rule.
    stype, _ = classify_series(
        _tags(modality="MR", descr="AXIAL DWI 4MM", DiffusionBValue=1000.0,
              image_type=["DERIVED", "SECONDARY", "OTHER"]),
        4,
    )
    assert stype == "DWI"


def test_send_to_rapid_is_an_acquisition_not_a_rapid_product():
    # "Ax DWI (SEND TO RAPID-3)" is a real DWI being *sent to* RAPID, not a RAPID
    # output. A substring match on "rapid" discarded 438 human-confirmed DWI
    # series as DERIVED. ORIGINAL/PRIMARY is the scanner asserting provenance and
    # must outrank the description lexicon.
    stype, rule = classify_series(
        _tags(modality="MR", descr="Ax DWI (SEND TO RAPID-3) (freq r/l)",
              image_type=["ORIGINAL", "PRIMARY", "OTHER"]),
        2,
    )
    assert stype == "DWI", rule


def test_original_primary_outranks_every_derived_keyword():
    # Same invariant, stated generally: no description keyword may override an
    # ORIGINAL/PRIMARY acquisition.
    for descr in ("Ax DWI send to RAPID", "CTA with MIP reformat request", "3D Lab protocol CTA"):
        _, rule = classify_series(
            _tags(descr=descr, kernel="STANDARD",
                  image_type=["ORIGINAL", "PRIMARY", "AXIAL"]),
            1, 200,
        )
        assert rule != "description-derived", descr


def test_rapid_summary_is_still_derived_even_with_a_bvalue():
    # The b-value exemption must not rescue post-processing output: a
    # "RAPID Summary" can carry a DiffusionBValue too. Description-derived is
    # therefore checked BEFORE the ImageType rule.
    stype, rule = classify_series(
        _tags(modality="MR", descr="RAPID Summary", DiffusionBValue=1000.0,
              image_type=["DERIVED", "SECONDARY", "OTHER"]),
        4,
    )
    assert stype is None
    assert rule == "description-derived"


def test_rapid_dwi_summary_is_not_a_dwi():
    # 3,806 series were labelled DWI by the old geometry-only classifier that are
    # actually RAPID post-processing summaries.
    stype, _ = classify_series(
        _tags(modality="MR", descr="RAPID DWI-PWI Summary",
              image_type=["DERIVED", "SECONDARY", "OTHER"]),
        6,
    )
    assert stype is None


def test_derived_primary_mpr_stays_classifiable():
    # DERIVED/PRIMARY is a reformat of a real acquisition — it must NOT be swept
    # into DERIVED along with the screen-saves, or we lose every MPR.
    stype, _ = classify_series(
        _tags(descr="VOLUME Head 3.000 CE", kernel="FC68",
              image_type=["DERIVED", "PRIMARY", "MPR"]),
        1,
    )
    assert stype != "DERIVED"


# --- Imaging plane: CT reformats are not acquisitions --------------------------

AXIAL = [1, 0, 0, 0, 1, 0]
CORONAL = [1, 0, 0, 0, 0, -1]
SAGITTAL = [0, 1, 0, 0, 0, -1]


@pytest.mark.parametrize("descr,kernel,iop", [
    ("CTA  2.0  MPR  Cor", "Bv40", CORONAL),
    ("CTA  2.0  MPR  Sag", "Bv40", SAGITTAL),
    ("SAG HEAD", "STANDARD", SAGITTAL),
    ("COR HEAD", "STANDARD", CORONAL),
    ("Head W/O  2.0  MPR  cor", "STANDARD", CORONAL),
])
def test_coronal_and_sagittal_ct_are_reformats(descr, kernel, iop):
    # A CT is always acquired axially; a cor/sag CT series is a reconstruction.
    stype, rule = classify_series(
        _tags(descr=descr, kernel=kernel, image_type=["DERIVED", "PRIMARY", "AXIAL"],
              ImageOrientationPatient=iop),
        1,
    )
    assert stype is None
    assert rule == "ct-reformat-non-axial"


def test_plane_comes_from_cosines_not_from_imagetype_or_description():
    # `CTA 2.0 MPR Cor` carries ImageType ...AXIAL... — the metadata lies. Only
    # ImageOrientationPatient tells the truth.
    stype, _ = classify_series(
        _tags(descr="CTA  2.0  MPR  Cor", kernel="Bv40",
              image_type=["DERIVED", "PRIMARY", "AXIAL", "CT_SOM5 MPR"],
              ImageOrientationPatient=CORONAL),
        1,
    )
    assert stype is None


def test_axial_ct_acquisition_is_unaffected():
    stype, _ = classify_series(
        _tags(descr="ROUTINE HEAD STD", kernel="STANDARD",
              image_type=["ORIGINAL", "PRIMARY", "AXIAL"],
              ImageOrientationPatient=AXIAL),
        1,
    )
    assert stype == "NCCT"


def test_axial_mpr_is_kept_as_an_acquisition():
    # Only cor/sag are reformats for our purposes; a thin-slice axial MPR is
    # still the series a rater reads.
    stype, _ = classify_series(
        _tags(descr="Head W/O  2.0  MPR  ax", kernel="STANDARD",
              image_type=["DERIVED", "PRIMARY", "MPR"],
              ImageOrientationPatient=AXIAL),
        1,
    )
    assert stype == "NCCT"


def test_sagittal_mr_is_NOT_a_reformat():
    # The plane rule is CT-only: sagittal T1 / coronal DWI are real MR acquisitions.
    stype, _ = classify_series(
        _tags(modality="MR", descr="cor DWI 4MM", DiffusionBValue=1000.0,
              image_type=["ORIGINAL", "PRIMARY", "OTHER"],
              ImageOrientationPatient=CORONAL),
        4,
    )
    assert stype == "DWI"


def test_oblique_reformat_caught_by_the_anchored_description_backstop():
    # A double-oblique "CTA 2.0 MPR Cor" has cosines tilted between planes, so
    # the dominant-axis test reads it as axial. The anchored name catches it.
    stype, rule = classify_series(
        _tags(descr="CTA  2.0  MPR  Cor", kernel="Bv40",
              image_type=["DERIVED", "PRIMARY", "AXIAL"],
              ImageOrientationPatient=[0.993, 0.098, -0.055, -0.109, 0.729, -0.676]),
        1,
    )
    assert stype is None
    assert rule == "ct-reformat-description"


def test_reformat_backstop_does_not_match_cor_inside_recon():
    # The bug this whole lexicon design exists to avoid: bare 'cor' matches
    # inside 'RECON'. 'ANGIO 1.25MM RECON' is a real CTA acquisition.
    stype, _ = classify_series(
        _tags(descr="ANGIO 1.25MM RECON", kernel="STANDARD",
              image_type=["ORIGINAL", "PRIMARY", "AXIAL"],
              ImageOrientationPatient=AXIAL),
        1,
    )
    assert stype == "CTA"


def test_missing_orientation_does_not_force_derived():
    stype, _ = classify_series(
        _tags(descr="ROUTINE HEAD STD", kernel="STANDARD",
              image_type=["ORIGINAL", "PRIMARY", "AXIAL"]),
        1,
    )
    assert stype == "NCCT"


# --- Contrast: the "Optimum Contrast" trap ------------------------------------


def test_optimum_contrast_is_a_blend_preset_not_iv_contrast():
    # Siemens names a dual-energy blend "Optimum Contrast#0 (Auto)". A naive
    # 'contrast' substring match reads this noncontrast series as enhanced.
    stype, rule = classify_series(
        _tags(descr="DE_Head  1.5  Q34s  3 Optimum Contrast#0 (Auto)",
              kernel=["Q34s", "3"], study="CT HEAD WO IV CONTRAST"),
        1,
    )
    assert stype is None
    assert rule == "de-optimum-blend-pending-review"


def test_study_description_resolves_unmarked_contrast_state():
    # 4,315 "unmarked" DE_Head series sit inside `CT HEAD WO IV CONTRAST` studies;
    # the parent study supplies the contrast state the series omits.
    _, rule = classify_series(
        _tags(descr="DE_Head  0.75  Hr32  3  F_0.5", kernel=["Hr32s", "3"],
              study="CT HEAD WO IV CONTRAST"),
        1,
    )
    assert rule == "de-blend-noncontrast-pending-review"


def test_w_and_wo_study_cannot_disambiguate_a_series():
    # A "W AND WO" study holds both phases — it must not resolve either way.
    _, rule = classify_series(
        _tags(descr="DE_Head  0.75  Hr32  3  F_0.5", kernel=["Hr32s", "3"],
              study="CT HEAD W AND WO IV CONTRAST BRAIN PERFUSION"),
        1,
    )
    assert rule == "de-blend-unknown-contrast-pending-review"


# --- Dual-energy: held unresolved pending clinical review ---------------------


@pytest.mark.parametrize("descr,expected_rule", [
    ("DE_Head  1.5  Qr32  3  A_80kV", "de-energy-bin-pending-review"),
    ("DE_Head  1.5  Qr32  3  B_Sn150kV", "de-energy-bin-pending-review"),
    ("DE_Head WC  0.75  Hr32  3  F_0.5", "de-blend-contrast-pending-review"),
])
def test_dual_energy_is_parked_with_a_granular_rule(descr, expected_rule):
    # Held unresolved deliberately (matching his exclusion of DE kernels) until
    # the family is clinically reviewed. The granular rule names are what let the
    # dry-run report price each policy before we commit to one.
    stype, rule = classify_series(_tags(descr=descr, kernel=["Qr32s", "3"]), 1)
    assert stype is None
    assert rule == expected_rule


# --- MR diffusion (new stage) -------------------------------------------------


def test_adc_maps_are_caught_despite_single_frame_per_position():
    # ADC/eADC carry a b-value but one frame per slice location, so the 2-14
    # frame DWI rule structurally misses them.
    stype, _ = classify_series(
        _tags(modality="MR", descr="ADC (10^-6 mm2/s)", DiffusionBValue=1000.0), 1
    )
    assert stype == "ADC"


def test_asl_is_excluded():
    stype, _ = classify_series(_tags(modality="MR", descr="AX 3D ASL (color)"), 1)
    assert stype is None


# --- MR angiography -----------------------------------------------------------


@pytest.mark.parametrize("descr", [
    "3DTOF COW MRA fast",
    "MRA COW",
    "Brain Ax 3D MRA_TOF",
    "AXIAL 3D TOF HEAD",
    "Ax 2D TOF FSPGR neck",     # 2D neck TOF counts too
    "AXIAL 2D TOF NECK",
])
def test_non_contrast_mra_is_tof(descr):
    stype, _ = classify_series(
        _tags(modality="MR", descr=descr, image_type=["ORIGINAL", "PRIMARY", "OTHER"]), 1
    )
    assert stype == "MRA_TOF"


@pytest.mark.parametrize("descr", [
    "MRA CAROTID+C",            # ContrastBolusAgent is EMPTY on these...
    "MRA CAROTID+C 1/2 dose",
    "Gad Neck Cor 3D MRA",      # ...and on these. Only the description says gado.
])
def test_gadolinium_mra_is_ce(descr):
    stype, rule = classify_series(
        _tags(modality="MR", descr=descr, image_type=["ORIGINAL", "PRIMARY", "OTHER"]), 1
    )
    assert stype == "MRA_CE", rule


def test_mra_detected_from_sequence_variant_when_description_is_silent():
    stype, _ = classify_series(
        _tags(modality="MR", descr="AXIAL HEAD", image_type=["ORIGINAL", "PRIMARY", "M"],
              SequenceVariant=["TOF", "MTC", "SP", "OSP"]),
        1,
    )
    assert stype == "MRA_TOF"


@pytest.mark.parametrize("descr,image_type", [
    # PJN: = projection, COL: = collapse. Both are MIPs of the MRA, and both are
    # DERIVED/PRIMARY (not DERIVED/SECONDARY), so only the explicit ImageType
    # token catches them. "pjn" used to sit in the LOCALIZER lexicon and typed
    # 1,221 of these as localizers.
    ("PJN:3DTOF COW MRA fast", ["DERIVED", "PRIMARY", "PROJECTION IMAGE", "VASCULAR"]),
    ("COL:MRA COW", ["DERIVED", "PRIMARY", "PROJECTION IMAGE", "COLLAPSE"]),
])
def test_mra_projections_are_derived_not_localizer_and_not_mra(descr, image_type):
    stype, rule = classify_series(_tags(modality="MR", descr=descr, image_type=image_type), 1)
    assert stype is None
    assert rule == "imagetype-projection"


def test_swi_is_not_mistaken_for_mra():
    # SWI is 3D GRE like TOF, but it is not angiography.
    stype, _ = classify_series(
        _tags(modality="MR", descr="3DEPI SWI-AX Neuromix",
              image_type=["ORIGINAL", "PRIMARY", "OTHER"]),
        1,
    )
    assert stype != "MRA_TOF"


# --- Catheter angiography -----------------------------------------------------


def test_xa_is_an_exclusion_not_a_type():
    # He has a DSA_description list but never wires it to an output column.
    stype, rule = classify_series(_tags(modality="XA", descr="RM Std Neuro"), None)
    assert stype is None
    assert rule == "modality-xa"


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
