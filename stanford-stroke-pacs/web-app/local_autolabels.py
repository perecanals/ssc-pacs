"""Web integration for installation-owned rule packages."""

import sys
from pathlib import Path

ingestion = str(Path(__file__).resolve().parents[1] / "image_ingestion_protocols")
if ingestion not in sys.path:
    sys.path.append(ingestion)

from autolabels import ANCHORS, RULES
from autolabels.runner import refresh


def present_timepoints(rows):
    offsets = {}
    for strategy in ANCHORS.values():
        for field, hours in strategy:
            offsets.setdefault(field, set()).add(bool(hours))
    for row in rows:
        row["timepoint_estimated"] = offsets.get(row.get("timepoint_anchor_source")) == {True}
        result = row.pop("_timepoint_result", None)
        if not result:
            continue
        rule = RULES.get((result["rule"], result["version"]))
        formatter = getattr(rule, "present", None)
        if formatter:
            lines, estimated = formatter(result["reason"], result["evidence"])
            row["timepoint_details"] = lines
            row["timepoint_estimated"] = estimated
        else:
            row["timepoint_details"] = [result["reason"].replace("_", " ")]


def refresh_quality(cur, level, label, study_uid):
    dependencies = []
    for rule in RULES.values():
        exclusion = getattr(rule, f"{level}_exclusion", None)
        if exclusion and exclusion[0] == label:
            dependencies.append((rule.name, rule.version))
    if not dependencies:
        return
    cur.execute(
        """SELECT d.name FROM dataset d WHERE EXISTS (
                SELECT FROM study_visibility v JOIN patient p USING(patient_key)
                WHERE v.studyinstanceuid=%s AND p.dataset=d.name) ORDER BY d.name FOR SHARE""",
        (study_uid,),
    )
    cur.execute(
        """SELECT DISTINCT p.subject_id FROM study_visibility v JOIN patient p USING(patient_key)
                JOIN dataset_autolabel_rule g ON g.dataset=p.dataset
                WHERE v.studyinstanceuid=%s AND (g.rule,g.version) IN %s""",
        (study_uid, tuple(dependencies)),
    )
    subjects = [row["subject_id"] for row in cur.fetchall()]
    if subjects:
        refresh(cur, subjects=subjects)
