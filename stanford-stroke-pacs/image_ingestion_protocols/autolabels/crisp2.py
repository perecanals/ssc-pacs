"""CRISP2 transfer timeline. All offsets are study time minus clinical time."""

from dataclasses import replace

from .model import Decision, Study, StudyRule

RECOGNITION = ("time_recognized", "onset_time", "time_normal")
CLOCKS = ("receiving_arrival_time", "femoral_sheath_time", "imaging_refer_bl_time", "imaging_receiv_bl_time")


def _classify(study: Study, *, twelve_hour_followup: bool) -> Decision:
    if study.acquired is None:
        return Decision(study.timepoint, "missing_study_time")

    def minutes(field):
        when = study.timestamp(field)
        return (study.acquired - when).total_seconds() / 60 if when is not None else None

    source = next((key for key in RECOGNITION if study.timestamp(key) is not None), None)
    since_recognition = minutes(source) if source else None
    arrival, puncture, referring, receiving = map(minutes, CLOCKS)
    evidence = dict(
        recognition_source=source,
        recognition_minutes=since_recognition,
        arrival_minutes=arrival,
        puncture_minutes=puncture,
        referring_minutes=referring,
        receiving_minutes=receiving,
    )
    outside_match = referring is not None and abs(referring) <= 60
    receiving_match = receiving is not None and abs(receiving) <= 30

    def result(value, reason):
        return Decision(value, reason, evidence)

    if study.timepoint != "BL":
        return result(study.timepoint, "general_timepoint")
    if since_recognition is not None and since_recognition < 0:
        return result("PRE", "before_recognition")
    if source is None and (arrival is None or abs(arrival) > 1440):
        return result("BL", "missing_recognition_outside_arrival_window")
    if puncture is not None and (puncture >= 0 or (arrival is not None and puncture > arrival)):
        return result("BL", "puncture_conflict")
    if arrival is None:
        direct = str(study.clinical.get("was_pt_transfer")).strip() in {"0", "0.0"}
        if direct and not outside_match:
            return result("BLT", "direct_presentation")
        return result("BL", "missing_arrival")
    if outside_match and receiving_match:
        return result("BL", "both_sites_match")
    if arrival >= 0:
        if outside_match:
            return result("BL", "referring_match_after_arrival")
        if puncture is not None:
            return result("BLT", "after_arrival_before_puncture")
        if twelve_hour_followup:
            if arrival <= 12 * 60:
                return result("BLT", "within_12h_after_arrival")
            return result("FU", "over_12h_after_arrival_without_puncture")
        if arrival <= 1440:
            return result("BLT", "within_day_after_arrival")
        return result("BL", "late_without_puncture")
    if receiving_match:
        if arrival >= -15:
            return result("BLT", "receiving_match_near_arrival")
        return result("BL", "receiving_match_before_arrival")
    if arrival >= -30:
        return result("BL", "pre_arrival_boundary")
    if arrival >= -1440:
        return result("BLO", "before_arrival")
    if outside_match:
        return result("BLO", "early_referring_match")
    return result("BL", "early_without_referring_match")


def classify(study: Study) -> Decision:
    """Version 1, retained for reproducing earlier assignments."""
    return _classify(study, twelve_hour_followup=False)


def classify_v2(study: Study) -> Decision:
    return _classify(study, twelve_hour_followup=True)


BASELINE_TIMEPOINT = StudyRule(
    name="crisp2_baseline_timepoint",
    version=1,
    output="effective_timepoint",
    clinical_fields=(*RECOGNITION, *CLOCKS, "was_pt_transfer"),
    evaluate=classify,
)

BASELINE_TIMEPOINT_V2 = replace(BASELINE_TIMEPOINT, version=2, evaluate=classify_v2)
