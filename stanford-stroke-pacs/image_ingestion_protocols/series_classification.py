"""Machine-owned classification, preference ranks and enrollment timepoints.

Series rules live in series_detection; ingestion and reclassification call the
same public API. Manual annotations are evaluation references, never runtime
inputs, and automatic classification never writes manual labels.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from series_detection import EMITTED_TYPES, classify_series, classify_series_batch  # noqa: F401

RULES_VERSION = "rules-v6"


def _norm(value: Any) -> str:
    return str(value).lower() if value is not None else ""


def _any_in(haystack: str, needles) -> bool:
    return any(n.lower() in haystack for n in needles if n)


# --- Preference ranking (his NCCT_1 / CTA_2 / ...) -----------------------------
#
# His labels are ranked within the owning enrollment (image_study.patient_key;
# Alembic 0028) — not the whole person, whose other enrollments may hold
# re-exported copies of the same series — so rank 1 is *the* NCCT/CTA/DWI to use.
# Ordering per modality_finder.py: CTA thinnest-slice first, NCCT thickest first,
# the rest chronological; tie-broken by his map_imagetype (original > secondary >
# derived), then time. SQL because the rank is a window over the owner's other
# series — ingest and reclassify call the same statement and cannot disagree.

ASSIGN_RANKS_SQL = """
WITH ranked AS (
    SELECT s.seriesinstanceuid,
           row_number() OVER (
               PARTITION BY s.patient_key, s.series_type
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
