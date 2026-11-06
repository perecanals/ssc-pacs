"""Readable series-family detection from DICOM metadata, without patient IDs or labels.

Description and modality lead. Repeated-position geometry identifies dynamic
acquisitions. Contrast, thickness and a small kernel vocabulary resolve static
CTs. ImageType supplies sequence/reconstruction tokens; whole-series plane
consensus protects diagnostic stacks containing a scout. Unknown inputs remain
unclassified, and axial MPR is eligible only as a study/family fallback.
"""

from __future__ import annotations

import re
from collections import defaultdict

EMITTED_TYPES = frozenset(
    {
        "NCCT",
        "CTA",
        "CTP",
        "CECT",
        "CT_BONE",
        "CT_MATERIAL",
        "DWI",
        "ADC",
        "PWI",
        "MRA_TOF",
        "MRA_CE",
        "FLAIR",
        "GRE",
        "SWI",
        "ASL_SOURCE",
        "ASL_CBF",
        "ASL_DELAY",
        "ASL_OTHER",
        "RAPID_SUMMARY",
        "DOCUMENT",
        "DSA",
    }
)


def _image_type(tags):
    value = tags.get("ImageType") or []
    return [str(v).upper() for v in ([value] if isinstance(value, str) else value)]


def _text(value):
    if isinstance(value, (list, tuple)):
        return " ".join(map(str, value)).lower()
    return str(value or "").lower()


def _matches(value, pattern):
    return bool(re.search(pattern, value, re.I))


def nonaxial_view(tags):
    """Recognize named coronal/sagittal views or verified whole-series geometry.

    Older stored tags may describe an embedded scout. Its plane must not reject
    a diagnostic series. Only the consensus aggregate is used for geometry.
    """
    desc = _text(tags.get("SeriesDescription")).replace("_", " ")
    image_type = " ".join(_image_type(tags)).lower()
    if _matches(desc + " " + image_type, r"\b(?:cor(?:onal)?|sag(?:ittal)?)\b"):
        return "nonaxial-description"
    if tags.get("_series_plane") in {"CORONAL", "SAGITTAL"} and not _projection(tags):
        return "nonaxial-orientation"
    return None


def _projection(tags):
    desc = _text(tags.get("SeriesDescription")).replace("_", " ")
    desc = re.sub(r"\bfor\s+mips?\b", "", desc)
    return _matches(
        desc + " " + " ".join(_image_type(tags)),
        r"\bmips?\b|\bpjn\b|projection",
    )


def _mpr(tags):
    desc = _text(tags.get("SeriesDescription")).replace("_", " ")
    # A source stack intended FOR MPR is still an ordinary acquisition.
    desc = re.sub(r"\bfor\s+(?:mpr|3d)\b", "", desc)
    return _matches(desc + " " + " ".join(_image_type(tags)), r"\bmpr\b|\breformat\w*")


def _axial(tags):
    return tags.get("_series_plane") == "AXIAL" or _matches(
        _text(tags.get("SeriesDescription")).replace("_", " "),
        r"\b(?:ax(?:ial)?|trueax|tra(?:nsversal)?)\b",
    )


def contrast_state(description):
    # Series-level statements outrank study headers and reused injection tags.
    if _matches(description, r"\bw\s*(?:and(?:/or)?|/|&)\s*w/?o\b"):
        return None
    if _matches(
        description,
        r"\bncct\b|\bnc\b|\bnon[ -]?con|\bwithout\b|\bw\s*/\s*o\b|\bwo\b|_wo\b|\bpre[ -]?con|\bnative\b",
    ):
        return False
    if _matches(
        description,
        r"\bwith(?:out)?\s+(?:iv\s+)?con|\bw\s*/\s*(?:con|$)|\bw\s+(?:iv\s+)?con|\bwc\b|_wc\b|\bce\b|\bpost[ -]?con|\+c\b|\bgad|\bhead\s+(?:with|w)\b",
    ):
        return True
    return None


def _classify_series_candidate(
    tags: dict, same_position_count: int | None = None, n_instances: int | None = None
) -> tuple[str | None, str]:
    """Return a per-series candidate; batch classification resolves study context."""
    tags = tags or {}
    mod = _text(tags.get("Modality")).upper()
    desc = _text(tags.get("SeriesDescription")).replace("_", " ")
    study = _text(tags.get("StudyDescription")).replace("_", " ")
    it = _image_type(tags)
    joined = " ".join(it).lower()
    kernel = "".join(_text(tags.get("ConvolutionKernel")).split())
    explicit_contrast = contrast_state(desc)
    agent = _text(tags.get("ContrastBolusAgent")).strip() not in ("", "none", "no", "0")
    n = n_instances or 0
    # Administrative documents and named processing products have their own types.
    if _matches(
        desc,
        r"pacs nomination|study acquired outside|reports? imported|outside (?:exam|report)|scanned documents",
    ):
        return ("DOCUMENT", "description-document")
    if (
        n_instances is not None
        and n <= 4
        and _matches(desc, r"^\d+(?:\s+ce)?(?:\s+summary)?$")
    ):
        return ("DOCUMENT", "small-document-series")
    if _matches(desc, r"\brapid\b|aif[/ ]vof") and not _matches(
        desc, r"\b(?:send|sent)\b|\bto rapid\b"
    ):
        # Only the consolidated report is a summary. Slice-by-slice threshold
        # products can themselves contain 'Summary' in their descriptions.
        if _matches(desc, r"\brapid\b.*\bsummary\b") and not _matches(
            desc, r"\bslices?\b|\bthresholds?\b"
        ):
            return ("RAPID_SUMMARY", "description-rapid-output")
        # Stop here: perfusion maps are not native perfusion acquisitions or ASL.
        return (None, "rapid-derived-output")
    if _matches(desc, r"\bscout\b|\btopo\w*|\blocaliz|\bsurview\b|3\s*plane\s*loc"):
        return (None, "localizer")
    if mod == "XA":
        return ("DSA", "modality-xa")
    if mod == "MR":
        # Named diagnostic MR families are more specific than repetition geometry.
        if (
            _matches(desc, r"\basl\b|easl|\bcbf\b|cerebral blood flow")
            or "asl" in joined
        ):
            if _matches(desc, r"not (?:for )?diagnostic|per del|mean pw|\bref\b"):
                t = "ASL_OTHER"
            elif _matches(desc, r"transit delay|\bdelay\b"):
                t = "ASL_DELAY"
            elif _matches(desc, r"\bcbf\b|cerebral blood flow") or "cbf" in joined:
                t = "ASL_CBF"
            else:
                t = "ASL_SOURCE"
            return (t, "mr-asl")
        if "flair" in desc:
            return ("FLAIR", "mr-flair")
        if _matches(desc, r"\bswi\b|susceptibility"):
            return ("SWI", "mr-swi")
        if same_position_count is not None and same_position_count >= 15:
            return ("PWI", "mr-dynamic-geometry")
        if _matches(desc, r"\bgre\b|t2\*"):
            return ("GRE", "mr-gre")
        if _matches(desc, r"\badc\b|_adc\b|eadc") or "adc" in joined:
            return ("ADC", "mr-adc")
        if _matches(desc, r"\bb[ =_-]*0\b"):
            return ("DWI", "mr-b0")
        if _matches(desc, r"\bpwi\b|\bmrp\b|perfusion|\bdsc\b"):
            return ("PWI", "mr-perfusion")
        if _matches(
            desc,
            r"\bdwi\b|_dwi|diffusion|diff.*trace|\bb[ =_-]*(?:[5-9]\d\d|[1-9]\d{3})\b|isodwi",
        ):
            return ("DWI", "mr-diffusion")
        if _matches(desc, r"tof|\bmra\b|\bcow\b|willis|angiogra"):
            ce = explicit_contrast is True or agent
            return ("MRA_CE" if ce else "MRA_TOF", "mr-angiography")
        if same_position_count is not None:
            if same_position_count >= 15:
                return ("PWI", "mr-dynamic-geometry")
            if same_position_count >= 2:
                return ("DWI", "mr-diffusion-geometry")
        return (None, "unresolved-mr")
    if mod != "CT":
        return (None, "unsupported-modality")
    if _matches(
        desc, r"\bdose\b|patient protocol|screen save|calibrat|3d lab|rendering"
    ):
        return (None, "ct-processing-output")
    if same_position_count is not None and same_position_count >= 14:
        return ("CTP", "ct-dynamic-geometry")
    if _matches(desc, r"iodine|\bwater\b|monoenergetic"):
        return ("CT_MATERIAL", "ct-material-map")
    if _matches(desc, r"perfusion|\bctp\b|\bvpct\b|\bslab\b"):
        return ("CTP", "ct-perfusion-description")
    if _matches(desc, r"test\s*bolus|monitoring|smart\s*prep|tracker|\bbolus\b"):
        return (None, "ct-test-bolus")
    arterial = _matches(desc, r"cta|angio|\bcow\b|willis|carotid|arterial|\bcar/cow\b")
    # Bones named explicitly are a reconstruction family; a sharp kernel alone
    # cannot override an explicit CTA protocol.
    if _matches(desc, r"\bbone\b") and not _matches(desc, r"\bcta\b|angio"):
        return ("CT_BONE", "ct-bone-description")
    if arterial:
        return ("CTA", "ct-angiography-description")
    if _matches(desc, r"\bmip\b") and _matches(study, r"cta|angio|stroke|perfusion"):
        return ("CTA", "ct-angiography-mip")
    if _matches(desc, r"c[ -]?spine|cervical|\bcs\b|\bspine\b"):
        return (None, "ct-spine-description")
    if _matches(desc, r"chest|abdomen|pelvis|\bcap\b"):
        return (None, "ct-body-description")
    # Sharp head reconstructions are bone, even when the protocol says NCCT.
    # _text handles DICOM multi-valued kernels such as ['J70h', '2'].
    bone_kernel = _matches(kernel, r"^(?:bone|h[678]0|hr59|hr[678]|j[78]0)")
    if bone_kernel:
        return ("CT_BONE", "ct-bone-kernel")
    if _matches(desc, r"\bvnc\b|\bvue\b"):
        return ("NCCT", "ct-virtual-noncontrast")
    if _matches(desc, r"\bncct\b|\bnon[ -]?con|\broutine head\b|\bhead std\b"):
        return ("NCCT", "ct-noncontrast-description")
    if explicit_contrast is False:
        return ("NCCT", "ct-noncontrast-description")
    # Hr59 is a sharp head reconstruction, not evidence of angiography.
    vascular_kernel = _matches(kernel, r"^(?:bv|br5|hr5[46]|i26|fc0[2358]|fc43|body_sharp)")
    kernel_type = "CTA" if vascular_kernel else None
    try:
        thickness = float(tags.get("SliceThickness") or 99)
    except (ValueError, TypeError):
        thickness = 99
    study_ce = contrast_state(study)
    contrast = (
        explicit_contrast
        if explicit_contrast is not None
        else (True if agent else study_ce)
    )
    # Postcontrast head is not automatically angiography. A thin vascular stack,
    # or angiography protocol context, supports CTA; thick CE head supports CECT.
    study_angio = _matches(study, r"\bcta\b|angio|\bcow\b|carotid")
    if contrast is True:
        if explicit_contrast is True and thickness >= 2 and kernel_type != "CTA":
            return ("CECT", "ct-postcontrast-head")
        if kernel_type == "CTA" or study_angio or (thickness <= 1.5 and n >= 80):
            return ("CTA", "ct-contrast-vascular-stack")
        if thickness <= 1.5:
            return (None, "ct-contrast-volume-uncertain")
        return ("CECT", "ct-postcontrast-head")
    if _matches(desc, r"de[ _-]?head") and contrast is not True:
        return ("NCCT", "ct-dual-energy-head")
    if kernel_type == "CTA":
        return ("CTA", "ct-vascular-kernel")
    if _matches(
        desc,
        r"head|brain|cerebral|\bax\b|axial|\bst[_ ]|soft tissue|dlir|\bstd\b|reformat",
    ):
        return ("NCCT", "ct-head-description")
    if study_angio and contrast is not False:
        return ("CTA", "ct-angiography-protocol")
    if _matches(study, r"head|brain") and n >= 10:
        return ("NCCT", "ct-head-study")
    return (None, "unresolved-ct")


def _eligible_view(tags, result):
    family, _ = result
    if family not in {None, "DOCUMENT", "RAPID_SUMMARY"} and _text(
        tags.get("Modality")
    ) in {"ct", "mr"}:
        if _projection(tags):
            return (None, "projection-output")
        if view_rule := nonaxial_view(tags):
            return (None, view_rule)
    return result


def classify_series(
    tags: dict, same_position_count: int | None = None, n_instances: int | None = None
) -> tuple[str | None, str]:
    """Return an eligible per-series candidate; batch detection adds study context."""
    return _eligible_view(
        tags or {}, _classify_series_candidate(tags, same_position_count, n_instances)
    )


def _acquisition_seconds(value) -> float | None:
    value = str(value or "").strip()
    if not re.fullmatch(r"\d{6}(?:\.\d+)?", value):
        return None
    hour, minute, second = int(value[:2]), int(value[2:4]), float(value[4:])
    if hour > 23 or minute > 59 or second >= 60:
        return None
    return hour * 3600 + minute * 60 + second


def classify_series_batch(rows: list[dict]) -> dict[str, tuple[str | None, str]]:
    """Classify series, then resolve CT context within each study.

    Each row supplies seriesinstanceuid, studyinstanceuid, tags and the usual
    same_position_count/n_instances_scanned aggregates. Study IDs only establish
    grouping; their contents and manual annotations never influence a decision.

    CT perfusion can be split into one volume per series UID. Matching volumes
    at >=14 distinct acquisition times within three minutes identify that case.
    A named CTA/NCCT can also identify ambiguous reconstructions with the same
    exact acquisition time. Conflicting or missing clocks do not propagate.
    """
    results = {
        row["seriesinstanceuid"]: _classify_series_candidate(
            row.get("tags"),
            row.get("same_position_count"),
            row.get("n_instances_scanned"),
        )
        for row in rows
    }
    volumes = defaultdict(list)
    acquisitions = defaultdict(list)
    series_contrast = {}
    for row in rows:
        tags = row.get("tags") or {}
        if _text(tags.get("Modality")) != "ct":
            continue
        time = _acquisition_seconds(tags.get("AcquisitionTime"))
        study = row.get("studyinstanceuid")
        if time is None or not study:
            continue
        uid = row["seriesinstanceuid"]
        acquisitions[(study, time)].append(uid)
        series_contrast[uid] = contrast_state(
            _text(tags.get("SeriesDescription")).replace("_", " ")
        )
        count = row.get("n_instances_scanned") or 0
        if row.get("same_position_count") != 1 or count < 10:
            continue
        if results[uid][1] in {
            "localizer",
            "ct-processing-output",
            "description-rapid-output",
            "rapid-derived-output",
            "ct-material-map",
            "ct-test-bolus",
            "ct-bone-kernel",
            "ct-bone-description",
        }:
            continue
        key = (
            study,
            " ".join(_text(tags.get("SeriesDescription")).split()),
            "".join(_text(tags.get("ConvolutionKernel")).split()),
            tags.get("SliceThickness"),
            count,
        )
        volumes[key].append((uid, time))
    for group in volumes.values():
        times = {time for _, time in group}
        if len(times) >= 14 and max(times) - min(times) <= 180:
            for uid, _ in group:
                results[uid] = ("CTP", "ct-temporal-volumes-across-series")

    weak_rules = {
        "ct-contrast-volume-uncertain",
        "ct-postcontrast-head",
        "ct-head-description",
        "ct-head-study",
        "ct-dual-energy-head",
        "ct-vascular-kernel",
        "ct-angiography-protocol",
        "ct-contrast-vascular-stack",
    }
    named_rules = {"ct-angiography-description", "ct-noncontrast-description"}
    for group in acquisitions.values():
        anchors = {results[uid][0] for uid in group if results[uid][1] in named_rules}
        if len(anchors) != 1:
            continue
        family = anchors.pop()
        for uid in group:
            # A copied study-wide clock is not evidence that a series explicitly
            # marked postcontrast is noncontrast.
            if family == "NCCT" and series_contrast[uid] is True:
                continue
            if results[uid][1] in weak_rules and results[uid][0] != family:
                results[uid] = (family, "ct-named-sibling-same-acquisition")
    # An excluded reconstruction can still describe its acquisition correctly.
    # Resolve study context before filtering views, so excluding a coronal CTA
    # does not erase the angiography evidence for its eligible axial siblings.
    results = {
        row["seriesinstanceuid"]: _eligible_view(
            row.get("tags") or {}, results[row["seriesinstanceuid"]]
        )
        for row in rows
    }
    regular = {
        (row["studyinstanceuid"], results[row["seriesinstanceuid"]][0])
        for row in rows
        if row.get("studyinstanceuid")
        and results[row["seriesinstanceuid"]][0] not in {None, "DOCUMENT", "RAPID_SUMMARY"}
        and not _mpr(row.get("tags") or {})
        and not _projection(row.get("tags") or {})
    }
    for row in rows:
        uid = row["seriesinstanceuid"]
        tags = row.get("tags") or {}
        family, _ = results[uid]
        if (
            _text(tags.get("Modality")) not in {"ct", "mr"}
            or family in {None, "DOCUMENT", "RAPID_SUMMARY"}
            or not _mpr(tags)
        ):
            continue
        if (row.get("studyinstanceuid"), family) in regular:
            results[uid] = (None, "axial-mpr-superseded")
        elif _axial(tags):
            results[uid] = (family, "axial-mpr-fallback")
        else:
            results[uid] = (None, "mpr-plane-unverified")
    return results
