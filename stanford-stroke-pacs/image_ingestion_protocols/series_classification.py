"""Rule-based series/study classification from DICOM metadata.

Single source of truth for both paths that assign a type: ingestion
(`image_ingestion_protocol.create_series_table`) and
`scripts/admin/reclassify_series_types.py`. If they diverge, the corpus becomes
inconsistent — which is the state this module exists to fix.

Every call returns `(type, rule)`; `RULES_VERSION` is stamped alongside, so a
classification can always be explained and safely recomputed.

Ported from a colleague's `DicomDetector` (a reference, never imported — it is
gitignored and moving). His kernel taxonomy and precedence are kept intact,
notably bone-before-NCCT. His Dutch description lexicons are not: they matched
~14% here, so the `_STANFORD_*` lists replace them.

Machine-owned. Independent of the human annotation labels of the same names
(`label_series_type_*` / `label_study_type_*`) — neither may be derived from the
other, in either direction.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from utils import identify_series_type

# Anchored on purpose: a bare "cor"/"sag" substring matches inside "RECON" — the
# bug in his EXCLUSION_description. No trailing \b ("coronals 3x2" is a real name).
_CT_REFORMAT_RE = re.compile(
    r"\bmpr\s*(cor|sag)|\b(coronal|sagittal)|^\s*(sag|cor)\s+head",
    re.IGNORECASE,
)

# Bump on any change to the lexicons or stage order; written to
# image_series.series_type_version / image_study.study_type_version.
#
# rules-v3: episode-aware timepoints (a patient with studies from two separate
# stroke episodes is split, each episode anchored independently) and a
# thrombectomy-study fallback anchor for episodes with no clinical anchor
# (non-LVO patients + the second episode of multi-episode ones). The
# acquisition-datetime construction (Acquisition -> Study) is factored into
# construct_acquisition_datetime and records which clock it used.
RULES_VERSION = "rules-v3"

# The complete set of types this module may emit — nothing else.
# The first five are his output columns (likely_ncct/cta/ctp/pwi/dwi); ADC and the
# MRA pair are sanctioned additions.
#
# Everything else in his taxonomy (bone, dual-energy, topogram, test bolus, RAPID
# output, projections, reformats, DSA) is an EXCLUSION, not a type: he sets those
# False on all five columns, we return None. The exclusion logic is kept in full —
# it is heavily tuned — but it mints no labels. A None is usually a decision, not
# a failure; `series_type_rule` says which exclusion fired.
EMITTED_TYPES = frozenset({
    "NCCT", "CTA", "CTP", "PWI", "DWI",   # his five
    "ADC", "MRA_TOF", "MRA_CE",           # sanctioned additions
})


# --- Kernel lexicons (from DicomDetector/default_information.py) --------------
#
# Matching is substring, case-insensitive, against the joined ConvolutionKernel
# (scanners emit either "STANDARD" or ["Hr32s", "3"] -> "Hr32s3").

CTA_KERNELS = (
    "B26f", "B31f", "B40f", "B46f", "Br38f2", "Br40d3", "Bv36d3", "Bv38f2", "Bv40d3",
    "Bv40f3", "Bv44d1", "Bv49d3", "Bv49f3", "DETAIL", "FC05", "H20s",
    "Hr40d3", "Hr49d3", "Hv38f3", "Hv40f3", "Hv40f4", "I26f2", "I26f3", "I26f4",
    "I30f2", "I30f4", "I40f2", "I31f2", "I31f3", "I40f3", "I46f", "I46f1", "I46f3",
    "IMR1,Routine", "IMR1,Soft Tissue", "I70f", "IMR2,Routine",
    "J30f2", "Hr38d4", "Hv40s3", "Hv40s", "I26f", "Br38f3", "BRAIN_CTA", "FC03",
    "FC02", "FC08", "FC41", "STANDARD2", "YC", "H10f", "D30f", "BODY_SHARP",
    "Br36s", "H40f", "FC43",
    # Stanford additions (Siemens Bv/Br sharp-vascular families seen in SIR):
    "Br54d", "Br54f", "Bv40",
)

NCCT_KERNELS = (
    "FC21", "FC68", "H31f", "H31s", "H40s", "H41s",
    "Hf38s", "Hf38s3", "Hr38s3", "Hr40f3", "Hr40s", "Hr40s3",
    "J30s2", "J30s4", "J40f4", "J40s2", "J40s3", "J40s5", "J45s4",
    "UB", "Br36s2", "Br36s3", "BRAIN_LCD", "FC26", "FC63", "FC64", "UC", "H41f",
    "Hf35s", "Hr38s",
)

# Matched BEFORE NCCT_KERNELS — this ordering is the point (bone windows must
# never be labelled NCCT).
NCCT_BONE_KERNELS = (
    "H60f", "Hr56f", "Hr56s3", "Hr64h1", "Hr64h2", "Hr68h", "J37f3", "J70h1",
    "J70h2", "J70h3", "FC30", "FC35", "YA", "Hr60f3", "Hr64h", "BONE", "BONEPLUS",
    "H70h", "H60s", "Hr68h3", "Hr69h3", "YB",
)

# Dual-energy recon kernels. At his site these were an exclusion; at Stanford the
# `DE_Head` family IS the routine head CT, so the kernel alone is not decisive —
# the description carries the contrast state. See _classify_ct_static.
DE_KERNELS = (
    "Q30f3", "Q33f3", "Q34s3", "Q40f3", "Qr40f2", "Qr40f3", "Hr38d3", "Qr40d3",
    "Hr36s3", "Hr38f3", "J45f2", "J45f3", "D26f", "D34f", "Q34f3", "Q40s3",
    "Qr32s2", "Qr40s2", "Qr40s3",
    # Stanford additions — the DE_Head workhorses, absent from his lists:
    "Qr32s", "Qr32d", "Hr32s", "Hr32d", "Hc40s", "Hc40d", "J30s",
)

TOPOGRAM_KERNELS = ("FL03", "FL04", "T20f", "Tr20f")
TESTBOLUS_KERNELS = ("B30f", "Br36f", "D20f", "B30s", "B31s")

# Kernels that serve several protocols — his logic defers to the description.
MULTI_MODAL_KERNELS = (
    "STANDARD", "H30f", "J30f4", "J40f3", "IMR1,Brain Routi", "Hr38s3",
    "J30s3", "SOFT", "H30s",
    # Stanford additions:
    "MD STND", "SOFT#", "MD SMTH",
)


# --- Description lexicons (Stanford; new) -------------------------------------
#
# Substring, case-insensitive. His Dutch lists ('hersenen', 'zonder contrast',
# 'schedel') are dropped wholesale — they matched 13-14% here.

_STANFORD_DERIVED = (
    "rapid", "3d lab", "dose report", "patient protocol", "summary",
    "screen save", "perfusion parameter maps", "aif/vof", "mip", "cpr",
    "vr ", "vrt", "rendering", "overlay", "calibrat",
)

# One DE acquisition fans out into ~7 series: two raw energy bins, a vendor blend,
# the blended soft-tissue image actually read, a bone window, and MPRs.
_DE_ENERGY_BIN = ("_80kv", "_100kv", "sn140kv", "sn150kv", "a_80", "b_sn")
_DE_BLEND = ("f_0.",)
# Siemens blend preset: says "Contrast" but means image contrast, not IV contrast.
# Matched explicitly so it can never reach _has_contrast.
_DE_OPTIMUM = ("optimum contrast",)
_STANFORD_LOCALIZER = (
    "topo", "scout", "localizer", "surview",
    # MR three-plane locators. Anchored ("plane loc", not bare "loc") so it
    # cannot match "block", "location", "col:" and friends.
    "plane loc", "3plane", "aahead_scout",
    # "pjn" was here, from his Dutch lexicon. At Stanford `PJN:` prefixes a
    # projection (MIP), not a localizer — now caught by _IMAGETYPE_DERIVED.
)

# Vendor stating "this is a rendering". PJN:/COL: MRA MIPs are DERIVED/PRIMARY, so
# the DERIVED/SECONDARY rule alone misses them.
_IMAGETYPE_DERIVED = ("PROJECTION IMAGE", "MIP", "MPR_COLLAPSE", "COLLAPSE")
_STANFORD_TESTBOLUS = ("monitoring", "testbolus", "test bolus", "smart prep", "tracker", "bolus")

_STANFORD_CTA = ("cta", "angio", "cow", "circle of willis", "carotid", "willis", "runoff")
_STANFORD_NCCT = (
    # Explicit.
    "ncct", "non con", "noncon", "without contrast", "wo con", "w/o con", "w/o",
    # Named protocols.
    "routine head", "head std", "axial head", "head wo", "brain wo", "cerebral",
    "head ct",
    # Bare anatomy ("Head 2.5mm", "Ax Brain Soft"). Broad on purpose; safe only
    # because CTA is matched first and NCCT refuses to fire with contrast present.
    # Do not reorder those two guards away.
    "head", "brain",
)
_STANFORD_BONE = ("bone",)
# Dual-energy material-decomposition and monoenergetic maps — reconstructions,
# not acquisitions. Stanford-only; his lists have no equivalent.
_STANFORD_DE_MATERIAL = (
    "iodine", "vnc", "water (", "(water)", " kev", "kev ", "mono", "gsi",
)
_STANFORD_PERFUSION = ("perfusion", "ctp", "vpct", "perf")

# MRA. TOF and gado-MRA are both ORIGINAL/PRIMARY 3D GRE, so geometry and
# ImageType cannot separate them from any other GRE volume — the description (or
# an explicit TOF SequenceVariant) is the signal, and contrast splits the two.
_STANFORD_MRA = ("tof", "mra", "cow", "willis", "angiogra")

# Contrast state as written into Stanford descriptions. "+c"/"gad" are essential:
# the gado MRA studies have an EMPTY ContrastBolusAgent (that tag is populated on
# only ~32% of series), so the description has to carry it.
_CONTRAST_PRESENT = (
    " w con", " w/ con", "with contrast", " ce", " wc ", "_wc", " w iv",
    "+c", "gad", "post con",
)
_CONTRAST_ABSENT = (" wo con", " w/o con", "without contrast", " wo ", "_wo", "_nc", " nc ")

# MR sequences that are not diffusion/perfusion targets (from his exclusion list).
_MR_EXCLUDE = ("asl", "fmri", "qsm", "swi")


def _norm(value: Any) -> str:
    return str(value).lower() if value is not None else ""


def _any_in(haystack: str, needles) -> bool:
    return any(n.lower() in haystack for n in needles if n)


def _kernel_text(tags: dict) -> str:
    kernel = tags.get("ConvolutionKernel")
    if kernel is None:
        return ""
    if isinstance(kernel, (list, tuple)):
        return "".join(str(k) for k in kernel).lower()
    return str(kernel).lower()


def _image_type(tags: dict) -> list[str]:
    value = tags.get("ImageType")
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).upper() for v in value]
    return [str(value).upper()]


def _image_plane(tags: dict) -> str | None:
    """AXIAL / CORONAL / SAGITTAL from ImageOrientationPatient, or None.

    The slice normal is the cross product of the row and column direction
    cosines; whichever patient axis it points along names the plane. Handles
    obliques and gantry tilt by taking the dominant axis.

    This is geometric truth, and it is the ONLY trustworthy plane signal here.
    Both alternatives lie: `CTA  2.0  MPR  Cor` is a coronal reformat whose
    ImageType reads DERIVED/PRIMARY/**AXIAL**/CT_SOM5 MPR, and descriptions are
    free text (the reference implementation's 'cor' token also matches inside
    'RECON').
    """
    iop = tags.get("ImageOrientationPatient")
    if not isinstance(iop, (list, tuple)) or len(iop) < 6:
        return None
    try:
        row = [float(v) for v in iop[:3]]
        col = [float(v) for v in iop[3:6]]
    except (TypeError, ValueError):
        return None

    normal = (
        row[1] * col[2] - row[2] * col[1],
        row[2] * col[0] - row[0] * col[2],
        row[0] * col[1] - row[1] * col[0],
    )
    dominant = max(range(3), key=lambda i: abs(normal[i]))
    return {0: "SAGITTAL", 1: "CORONAL", 2: "AXIAL"}[dominant]


def _has_contrast(description: str, tags: dict) -> bool | None:
    """True / False / None (unknown).

    SeriesDescription first; then the parent StudyDescription, which at Stanford
    routinely carries the contrast state the series omits (4,315 "unmarked"
    DE_Head series sit inside studies titled `CT HEAD WO IV CONTRAST`); then
    ContrastBolusAgent, which is non-empty on only ~32% of genuine CT.
    """
    if _any_in(description, _DE_OPTIMUM):
        return False  # vendor blend preset, not IV contrast — see _DE_OPTIMUM
    if _any_in(description, _CONTRAST_ABSENT):
        return False
    if _any_in(description, _CONTRAST_PRESENT):
        return True

    study = _norm(tags.get("StudyDescription"))
    if study:
        # "W AND WO" studies hold both phases — the study cannot disambiguate a
        # series inside it, so fall through rather than guess.
        if "w and wo" not in study:
            if _any_in(study, ("wo iv contrast", "wo contrast", "without contrast", "w/o contrast")):
                return False
            if _any_in(study, ("w iv contrast", "w contrast", "with contrast")):
                return True

    agent = str(tags.get("ContrastBolusAgent") or "").strip()
    if agent:
        return True
    return None


# --- Stage 1: derived / localizer (new — DicomDetector has no equivalent) -----


def _classify_non_acquisition(
    image_type: list[str], description: str, tags: dict
) -> tuple[str, str] | None:
    if "LOCALIZER" in image_type or _any_in(description, _STANFORD_LOCALIZER):
        return None, "imagetype-localizer"

    # A projection/MIP is derived by definition, whatever else ImageType says —
    # so this is checked before the ORIGINAL/PRIMARY guard below.
    if any(token in image_type for token in _IMAGETYPE_DERIVED):
        return None, "imagetype-projection"

    # ORIGINAL/PRIMARY is the scanner asserting "I acquired this". That outranks
    # every description heuristic below, and the guard is load-bearing: 438
    # human-confirmed DWI series are named "Ax DWI (SEND TO RAPID-3)" — they are
    # *sent to* RAPID, not *produced by* it — and a substring match on "rapid"
    # was throwing every one of them away as DERIVED. (Exactly the failure mode
    # of the reference implementation's EXCLUSION_description, where 'cor'
    # matches inside 'RECON'. Description lexicons must never override the
    # vendor's explicit statement of provenance.)
    #
    # Localizers are ORIGINAL/PRIMARY too, but they are already returned above.
    is_acquisition = image_type[:2] == ["ORIGINAL", "PRIMARY"]

    # Post-processing products name themselves (RAPID, 3D Lab, dose reports).
    # Checked BEFORE the ImageType rule below, because a "RAPID Summary" can
    # itself carry a DiffusionBValue and the b-value exemption would otherwise
    # rescue it as a real diffusion series.
    if not is_acquisition and _any_in(description, _STANFORD_DERIVED):
        return None, "description-derived"

    # DERIVED/SECONDARY is the vendor's own statement that this is a picture of a
    # picture: MIPs, rotating projections, screen saves, dose sheets.
    # DERIVED/PRIMARY is left alone — those are MPRs of a real acquisition.
    if image_type[:2] == ["DERIVED", "SECONDARY"]:
        # ...except that trace / isotropic DWI and ADC maps are *computed* from
        # the raw diffusion directions, so scanners legitimately stamp them
        # DERIVED\SECONDARY — while they remain the diffusion image a rater
        # actually reads. A carried DiffusionBValue is proof of provenance that
        # outranks ImageType. Without this, 12 genuine DWI/ADC series in the live
        # corpus (AXIAL DWI 4MM, b1000, IsoDWI, ...) were silently lost as DERIVED.
        if tags.get("DiffusionBValue") is None:
            return None, "imagetype-derived-secondary"

    return None


# --- Stage 3: CT static (his kernel precedence + Stanford descriptions) -------


# His per-identifier minimums (cta_identifier min_files=80, ncct min_files=10).
# Per type, not global — his identifiers are independent columns.
CTA_MIN_INSTANCES = 80
NCCT_MIN_INSTANCES = 10


def _meets_minimum(n_instances: int | None, minimum: int) -> bool:
    # A missing count defaults to passing, as in his `row.get("nfiles", min+100)`.
    return n_instances is None or n_instances >= minimum


def _gate_cta(rule: str, n_instances: int | None) -> tuple[str | None, str]:
    if not _meets_minimum(n_instances, CTA_MIN_INSTANCES):
        return None, f"{rule}-below-min-instances"
    return "CTA", rule


def _gate_ncct(rule: str, n_instances: int | None) -> tuple[str | None, str]:
    if not _meets_minimum(n_instances, NCCT_MIN_INSTANCES):
        return None, f"{rule}-below-min-instances"
    return "NCCT", rule


def _classify_ct_static(
    kernel: str, description: str, tags: dict, n_instances: int | None = None
) -> tuple[str | None, str]:
    # CT is always acquired axially, so a non-axial CT series is a reformat.
    # CT-only: sagittal T1 / coronal DWI are genuine MR acquisitions. Axial MPRs
    # are kept — a thin-slice axial recon is still what a rater reads.
    if _image_plane(tags) in ("CORONAL", "SAGITTAL"):
        return None, "ct-reformat-non-axial"

    # Backstop for oblique reformats, whose tilted cosines read as axial.
    if _CT_REFORMAT_RE.search(description):
        return None, "ct-reformat-description"

    if _any_in(kernel, TOPOGRAM_KERNELS):
        return None, "kernel-topogram"
    if _any_in(kernel, TESTBOLUS_KERNELS) or _any_in(description, _STANFORD_TESTBOLUS):
        return None, "kernel-or-description-testbolus"

    # Bone before NCCT — his judgement, preserved verbatim.
    if _any_in(kernel, NCCT_BONE_KERNELS) or _any_in(description, _STANFORD_BONE):
        return None, "kernel-bone"

    # Material-decomposition / monoenergetic maps are reconstructions of a
    # dual-energy acquisition, not an acquisition — classify before the kernel
    # lists, since they share the parent scan's kernel.
    if _any_in(description, _STANFORD_DE_MATERIAL):
        return None, "description-de-material"

    if _any_in(description, _STANFORD_PERFUSION):
        # Geometry (stage 2) already caught true CTP. A perfusion-named series
        # that is NOT multi-frame is a derived perfusion product.
        return None, "description-perfusion-derived"

    if _any_in(kernel, CTA_KERNELS):
        return _gate_cta("kernel-cta", n_instances)
    if _any_in(kernel, NCCT_KERNELS):
        return _gate_ncct("kernel-ncct", n_instances)

    # Dual-energy (~10k series). His DE_kernels bucket is an exclusion — neither
    # NCCT nor CTA — and we hold that pending clinical review of 4-0743 / 4-0876.
    # Each variant keeps its own rule name so the dry-run can price any future
    # policy. The bone check above fires first, so a DE bone window is already
    # excluded by that.
    if _any_in(kernel, DE_KERNELS) or "de_head" in description or "de spine" in description:
        if _any_in(description, _DE_ENERGY_BIN):
            return None, "de-energy-bin-pending-review"
        if _any_in(description, _DE_OPTIMUM):
            return None, "de-optimum-blend-pending-review"
        if _any_in(description, _DE_BLEND):
            contrast = _has_contrast(description, tags)
            if contrast is False:
                return None, "de-blend-noncontrast-pending-review"
            if contrast is True:
                return None, "de-blend-contrast-pending-review"
            return None, "de-blend-unknown-contrast-pending-review"
        return None, "de-other-pending-review"

    # Multi-modal or unknown kernel -> description decides (his stage 5/7).
    # CTA first: "CTA HEAD NECK" must not be claimed by the NCCT rule's bare
    # "head" token.
    if _any_in(description, _STANFORD_CTA):
        return _gate_cta("description-cta", n_instances)

    if _any_in(description, _STANFORD_NCCT):
        # An NCCT is *by definition* non-contrast. The description lexicon is
        # deliberately broad ("head", "brain"), so this guard is what keeps a
        # contrast-enhanced head out of the NCCT cohort.
        if _has_contrast(description, tags) is True:
            return None, "ncct-description-but-contrast-present"
        return _gate_ncct("description-ncct", n_instances)

    return None, "unresolved-ct"


# --- Stage 4: MR (new — his rules only see frame counts) ----------------------


def _is_mra(description: str, tags: dict) -> bool:
    """MR angiography: named as such, or the sequence variant says TOF."""
    if _any_in(description, _STANFORD_MRA):
        return True
    variant = tags.get("SequenceVariant")
    if variant is None:
        return False
    if isinstance(variant, (list, tuple)):
        return any("TOF" in str(v).upper() for v in variant)
    return "TOF" in str(variant).upper()


def _classify_mr_static(description: str, tags: dict) -> tuple[str | None, str]:
    if _any_in(description, _MR_EXCLUDE):
        return None, "mr-excluded-sequence"

    # ADC / eADC maps carry a b-value but only one frame per slice location, so
    # his 2-14-frame DWI rule structurally misses them.
    if tags.get("DiffusionBValue") is not None:
        if "adc" in description:
            return "ADC", "mr-bvalue-adc"
        return "DWI", "mr-bvalue-dwi"
    if "adc" in description:
        return "ADC", "description-adc"

    # MRA. Checked after diffusion (they do not overlap) and after the derived /
    # projection filter, so the MIPs of an MRA are already DERIVED and only the
    # source volume reaches here.
    if _is_mra(description, tags):
        if _has_contrast(description, tags) is True:
            return "MRA_CE", "mr-angio-contrast"
        return "MRA_TOF", "mr-angio-tof"

    return None, "unresolved-mr"


# --- Entry points -------------------------------------------------------------


def classify_series(
    tags: dict,
    same_position_count: int | None,
    n_instances: int | None = None,
) -> tuple[str | None, str]:
    """Return `(series_type, rule)` for one series.

    `tags` is the representative-instance blob from `series_dicom_tags.tags`;
    `same_position_count` and `n_instances` are the cross-instance aggregates
    from the same row. Both ingestion and the reclassify CLI call exactly this.

    The returned type is always one of EMITTED_TYPES, or None. A None is NOT a
    failure — it is usually a deliberate *exclusion*, and `rule` says which
    (`kernel-bone`, `kernel-topogram`, `ct-reformat-non-axial`, `modality-xa`...).
    Read the rule before concluding a series is unclassified.
    """
    tags = tags or {}
    modality = str(tags.get("Modality") or "").upper()
    description = _norm(tags.get("SeriesDescription"))
    image_type = _image_type(tags)
    kernel = _kernel_text(tags)

    non_acq = _classify_non_acquisition(image_type, description, tags)
    if non_acq:
        return non_acq

    # Geometry first for the dynamic families — a CTP is a CTP whatever its kernel.
    geometric = identify_series_type(modality, same_position_count, description)
    if geometric:
        return geometric, "geometry-same-position-count"

    if modality == "CT":
        return _classify_ct_static(kernel, description, tags, n_instances)
    if modality == "MR":
        return _classify_mr_static(description, tags)
    if modality == "XA":
        # Catheter angiography. He has a DSA_description list but never wires it
        # to an output column, so XA is an exclusion here too, not a type.
        return None, "modality-xa"

    return None, f"unhandled-modality-{modality.lower() or 'unknown'}"


# --- Preference ranking (his NCCT_1 / CTA_2 / ...) -----------------------------
#
# His labels are ranked within a patient, so rank 1 is *the* NCCT/CTA/DWI to use.
# Ordering per modality_finder.py: CTA thinnest-slice first, NCCT thickest first,
# the rest chronological; tie-broken by his map_imagetype (original > secondary >
# derived), then time. SQL because the rank is a window over the patient's other
# series — ingest and reclassify call the same statement and cannot disagree.

ASSIGN_RANKS_SQL = """
WITH ranked AS (
    SELECT s.seriesinstanceuid,
           row_number() OVER (
               PARTITION BY s.patient_id, s.series_type
               ORDER BY
                   CASE WHEN s.series_type = 'CTA'  THEN t.slice_thickness END ASC  NULLS LAST,
                   CASE WHEN s.series_type = 'NCCT' THEN t.slice_thickness END DESC NULLS LAST,
                   CASE
                       WHEN t.image_type ILIKE '%%SECOND%%' THEN 2
                       WHEN t.image_type ILIKE '%%DERIV%%'  THEN 3
                       WHEN t.image_type ILIKE '%%PRIMA%%'
                         OR t.image_type ILIKE '%%ORIG%%'   THEN 1
                       ELSE 4
                   END,
                   s.acquisitiondatetime ASC NULLS LAST,
                   s.seriesinstanceuid
           ) AS rnk
    FROM image_series s
    JOIN series_dicom_tags t USING (seriesinstanceuid)
    WHERE s.series_type IS NOT NULL
)
UPDATE image_series s
   SET series_type_rank = r.rnk,
       series_label     = s.series_type || '_' || r.rnk
  FROM ranked r
 WHERE r.seriesinstanceuid = s.seriesinstanceuid
"""

CLEAR_RANKS_SQL = """
UPDATE image_series
   SET series_type_rank = NULL, series_label = NULL
 WHERE series_type IS NULL
   AND (series_type_rank IS NOT NULL OR series_label IS NOT NULL)
"""


# --- Study level --------------------------------------------------------------
#
# Stanford StudyDescriptions are rich and need no clinical join:
#   "CT HEAD WO IV CONTRAST" / "NIR CEREBRAL ANGIOGRAPHY WITH THROMBECTOMY" /
#   "MR BRAIN W AND WO IV CONTRAST STROKE" / "CT HEAD ... BRAIN PERFUSION ..."
# This replaces the dead Spanish classifier ("Trombectomia"/"Cabeza"), which was
# a remnant of a different site.

_STUDY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Order matters: a stroke-protocol CT study is titled "... BRAIN PERFUSION
    # CT HEAD NECK ANGIOGRAPHY ..." and must not be claimed by the CTA rule, and
    # a thrombectomy study mentions angiography by definition.
    ("THROMBECTOMY", ("thrombectomy", "cerebral angiography", "thrombec", "thromec", "neuro angiogram")),
    ("CT_STROKE_PROTOCOL", ("perfusion",)),
    ("CTA", ("angiography", "angiogram", "cta", " ang ")),
    ("MR_BRAIN", ("mr brain", "mri brain", "mr head", "mr diagnosis stroke", "mr stroke")),
    ("CT_HEAD", ("ct head", "ct brain", "head ct", "ct stroke head")),
)


def classify_study(study_description: str | None, series_types: list[str] | None = None) -> tuple[str | None, str]:
    """Return `(study_type, rule)` from the StudyDescription.

    `series_types` (the study's classified series) is accepted for future use as
    a tie-breaker but is deliberately unused today — StudyDescription alone is
    unambiguous on this corpus, and a series-derived fallback would couple the
    two axes in a way that makes a series-rule change silently move study types.
    """
    description = _norm(study_description)
    if not description:
        return None, "no-study-description"

    for study_type, needles in _STUDY_RULES:
        if _any_in(description, needles):
            return study_type, f"description-{study_type.lower()}"

    return None, "unresolved-study"


# --- Timepoint (BL / THROMBECTOMY / FU) ---------------------------------------
#
# THE ANCHOR IS FEMORAL-SHEATH PUNCTURE, NOT STROKE ONSET. `BL` means
# pre-thrombectomy, not post-onset. `patient.stroke_date` is a different clock and
# is deliberately not used — mixing them would make our labels incomparable with
# the reference implementation's.
#
# Anchor precedence and the +5h / +10h offsets are ported verbatim from that
# implementation (get_metadata.py: `time_dct = {**recognized, **arrival, **evt}`,
# where a later key wins). Only ~59% of clinical rows carry a recorded
# `femoral_sheath_time`, so the other two are *estimates* — which is why the
# source is recorded alongside the answer.

_ANCHOR_PRECEDENCE: tuple[tuple[str, float], ...] = (
    ("femoral_sheath_time", 0.0),        # the recorded puncture time
    ("receiving_arrival_time", 5.0),     # estimate: arrival + 5h
    ("time_recognized", 10.0),           # estimate: recognition + 10h
)


def _parse_clinical_datetime(value):
    """Parse a clinical time cell ('YYYY-MM-DD HH:MM[:SS]') -> datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return None if value != value else value  # NaT is not equal to itself
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def resolve_event_anchor(clinical_row: dict | None) -> tuple[datetime | None, str | None]:
    """Return `(anchor_datetime, source_column)` for one patient's clinical row.

    `clinical_row` maps the `clinical_data` column names to values. Returns
    `(None, None)` when no anchor column is populated — 26% of our patients, who
    get a NULL timepoint rather than a guess.
    """
    if not clinical_row:
        return None, None

    for column, offset_hours in _ANCHOR_PRECEDENCE:
        parsed = _parse_clinical_datetime(clinical_row.get(column))
        if parsed is not None:
            return parsed + timedelta(hours=offset_hours), column

    return None, None


def classify_timepoint(
    acquisition_datetime,
    anchor_datetime: datetime | None,
    study_type: str | None = None,
) -> tuple[str | None, float | None, str]:
    """Return `(timepoint, hours_to_event, rule)` for one study.

    `hours_to_event` is signed: negative before the anchor, positive after. It is
    what lets a follow-up study be *selected* (the reference implementation picks
    follow-up DWI in 8-30h / 4-72h / 72-120h windows); a bare BL/FU flag cannot.
    """
    # The procedure study identifies itself, and does so even for the 26% of
    # patients with no clinical anchor at all.
    if study_type == "THROMBECTOMY":
        hours = _hours_between(acquisition_datetime, anchor_datetime)
        return "THROMBECTOMY", hours, "study-type-thrombectomy"

    acquired = _parse_clinical_datetime(acquisition_datetime)
    if acquired is None:
        return None, None, "no-acquisition-time"
    if anchor_datetime is None:
        return None, None, "no-clinical-anchor"

    hours = _hours_between(acquired, anchor_datetime)
    if acquired < anchor_datetime:
        return "BL", hours, "before-anchor"
    return "FU", hours, "at-or-after-anchor"


def _hours_between(acquisition_datetime, anchor_datetime: datetime | None) -> float | None:
    acquired = _parse_clinical_datetime(acquisition_datetime)
    if acquired is None or anchor_datetime is None:
        return None
    return (acquired - anchor_datetime).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Acquisition-datetime construction (single source of truth for the clock the
# timepoint logic compares against).
#
# Prefer the acquisition clock, then fall back to the study clock. ~16% of series
# carry no AcquisitionDate(Time) tag; StudyDate is present on 100% and is the
# study *encounter* day, which is what BL/FU (a day-resolution axis) needs.
#
# Content/Series dates are deliberately NOT probed. They are acquisition-proximate
# for a native acquisition, but for a DERIVED series (RAPID output, MIP, MPR — a
# large fraction of the corpus) ContentDate/SeriesDate is the day the derivative
# was *computed*, often weeks or months after the scan. Preferring them mis-dates
# those series and manufactures spurious second "episodes" (e.g. a 2024-09-30 scan
# whose RAPID maps carry a 2025-01-10 ContentDate). StudyDate is immune to that.
#
# Each entry is `(source_token, date_keyword, time_keyword_or_None)`; a None time
# keyword means the date keyword is a combined DICOM DT (only AcquisitionDateTime
# (0008,002A) exists as one).
_ACQ_DATETIME_PRECEDENCE: tuple[tuple[str, str, str | None], ...] = (
    ("acquisition", "AcquisitionDateTime", None),
    ("acquisition", "AcquisitionDate", "AcquisitionTime"),
    ("study", "StudyDate", "StudyTime"),
)


def _parse_dicom_dt(value) -> datetime | None:
    """Parse a DICOM DT string (YYYYMMDDHHMMSS[.ffffff][&ZZXX]) -> datetime.

    Fractional seconds and any timezone suffix are dropped (consistent with the
    prior pipeline, which did `.split('.')[0]`); we key on the calendar/clock
    fields only. Needs at least a full date (8 digits)."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        return None
    digits = (digits + "000000")[:14]  # pad a bare date to midnight
    try:
        return datetime.strptime(digits, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _parse_dicom_date_time(date_value, time_value) -> datetime | None:
    """Combine a DICOM DA (YYYYMMDD) and TM (HHMMSS[.ffffff]) -> datetime."""
    date_digits = re.sub(r"\D", "", str(date_value or ""))
    if len(date_digits) < 8:
        return None
    time_digits = re.sub(r"\D", "", str(time_value or ""))
    time_digits = (time_digits + "000000")[:6] if time_digits else "000000"
    try:
        return datetime.strptime(date_digits[:8] + time_digits, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def construct_acquisition_datetime(tags) -> tuple[datetime | None, str | None]:
    """Return `(datetime, source_token)` for one series' acquisition clock.

    `tags` maps DICOM keyword -> raw string value (from a pydicom dataset at
    ingest, or the `series_dicom_tags.tags` jsonb at backfill). Walks
    `_ACQ_DATETIME_PRECEDENCE` and returns the first that parses, along with a
    short source token (`acquisition|content|series|study`). `(None, None)` when
    nothing parses."""
    for source, date_keyword, time_keyword in _ACQ_DATETIME_PRECEDENCE:
        if time_keyword is None:
            parsed = _parse_dicom_dt(tags.get(date_keyword))
        else:
            parsed = _parse_dicom_date_time(tags.get(date_keyword), tags.get(time_keyword))
        if parsed is not None:
            return parsed, source
    return None, None


# ---------------------------------------------------------------------------
# Episode detection + per-episode anchoring.
#
# A handful of patients (the `11-*` cohort, ~15 in total) carry studies from two
# distinct stroke episodes months apart. A single per-patient clinical anchor
# then scores one episode's imaging against the OTHER episode's puncture, giving
# nonsensical hours_to_event and a whole episode mislabelled BL. We split a
# patient's studies into episodes by a large inter-study gap, then anchor each
# episode on its own: the clinical puncture for the episode it falls in, else the
# episode's own thrombectomy (XA) study.

# Gap (days) between consecutive studies that starts a new episode. The real
# multi-episode patients sit at >90-day gaps while intra-episode follow-up tops
# out well under 30 days, so any threshold in 30-90 isolates the same set; 45
# sits comfortably in that band.
DEFAULT_EPISODE_GAP_DAYS = 45


def assign_episodes(
    acquisition_datetimes, gap_days: int = DEFAULT_EPISODE_GAP_DAYS
) -> list[int | None]:
    """Return a 1-based episode index per input datetime, aligned to input order.

    Studies are ordered by time; a gap greater than `gap_days` to the previous
    study starts a new episode. Entries that do not parse to a datetime get
    `None` (cannot be placed)."""
    parsed = [(_parse_clinical_datetime(dt), i) for i, dt in enumerate(acquisition_datetimes)]
    placed = sorted((p for p in parsed if p[0] is not None), key=lambda p: p[0])

    episodes: dict[int, int] = {}
    episode = 0
    prev: datetime | None = None
    for dt, original_index in placed:
        if prev is None or (dt - prev) > timedelta(days=gap_days):
            episode += 1
        episodes[original_index] = episode
        prev = dt

    return [episodes.get(i) for i in range(len(acquisition_datetimes))]


def _nearest_episode(episode_windows: dict[int, tuple[datetime, datetime]], anchor: datetime) -> int:
    """Which episode a clinical anchor belongs to: the one whose [min,max] window
    contains it, else the temporally nearest."""
    best_episode, best_distance = None, None
    for episode, (low, high) in episode_windows.items():
        if low <= anchor <= high:
            distance = timedelta(0)
        else:
            distance = min(abs(anchor - low), abs(anchor - high))
        if best_distance is None or distance < best_distance:
            best_episode, best_distance = episode, distance
    return best_episode


def _thrombectomy_anchor(episode_studies) -> tuple[datetime | None, str | None]:
    """Anchor an episode on its earliest THROMBECTOMY study, if any."""
    thrombectomies = [
        s for s in episode_studies
        if s["study_type"] == "THROMBECTOMY" and s["datetime"] is not None
    ]
    if not thrombectomies:
        return None, None
    return min(thrombectomies, key=lambda s: s["datetime"])["datetime"], "thrombectomy_study"


def assign_patient_timepoints(
    studies, clinical_row: dict | None, gap_days: int = DEFAULT_EPISODE_GAP_DAYS
) -> dict[str, dict]:
    """Episode + timepoint for every study of one patient.

    `studies`: iterable of dicts with `studyinstanceuid`, `acquisition_datetime`
    (a datetime / pandas Timestamp / parseable string / None) and `study_type`.
    `clinical_row`: the patient's `clinical_data` row (or None).

    Returns `{studyinstanceuid: {episode, timepoint, hours_to_event,
    timepoint_anchor_source, timepoint_rule}}`.

    Anchor per episode: the clinical puncture (`resolve_event_anchor`) for the
    single episode it falls in, else that episode's own thrombectomy study, else
    none (NULL timepoint — no guess)."""
    normalized = [
        {
            "suid": s["studyinstanceuid"],
            "datetime": _parse_clinical_datetime(s.get("acquisition_datetime")),
            "study_type": s.get("study_type"),
        }
        for s in studies
    ]

    result: dict[str, dict] = {}
    # Studies with no acquisition clock cannot be placed or scored.
    for s in normalized:
        if s["datetime"] is None:
            result[s["suid"]] = {
                "episode": None, "timepoint": None, "hours_to_event": None,
                "timepoint_anchor_source": None, "timepoint_rule": "no-acquisition-time",
            }
    placed = [s for s in normalized if s["datetime"] is not None]
    if not placed:
        return result

    episode_indices = assign_episodes([s["datetime"] for s in placed], gap_days)
    by_episode: dict[int, list] = {}
    for s, ep in zip(placed, episode_indices, strict=True):
        s["episode"] = ep
        by_episode.setdefault(ep, []).append(s)

    clinical_anchor, clinical_source = resolve_event_anchor(clinical_row)
    clinical_episode = None
    if clinical_anchor is not None:
        windows = {
            ep: (min(s["datetime"] for s in members), max(s["datetime"] for s in members))
            for ep, members in by_episode.items()
        }
        clinical_episode = _nearest_episode(windows, clinical_anchor)

    for episode, members in by_episode.items():
        if clinical_anchor is not None and episode == clinical_episode:
            anchor, source = clinical_anchor, clinical_source
        else:
            anchor, source = _thrombectomy_anchor(members)
        for s in members:
            timepoint, hours, rule = classify_timepoint(s["datetime"], anchor, s["study_type"])
            result[s["suid"]] = {
                "episode": episode,
                "timepoint": timepoint,
                "hours_to_event": hours,
                # Source is meaningful only when a timepoint was actually assigned.
                "timepoint_anchor_source": source if timepoint is not None else None,
                "timepoint_rule": rule,
            }
    return result
