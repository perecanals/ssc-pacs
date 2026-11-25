# Dataset-specific automatic study labels

The ingestion pipeline computes general series types, study types, and timepoints. Each dataset can additionally enable versioned study rules. An empty registration runs no tailored rules. The admin CLI can export both the general timepoint and its dataset-specific refinement. The PACS browser’s **Auto Timepoint** column shows the effective value for the selected enrollment. Hover over a refined value to see its general timepoint. Flat study and series lists prefer the visible owner, then the first visible member by enrollment key, within the selected dataset. Disabling the effective-timepoint rule restores the general display.

## Run a rule

Run from the stack root using the PACS Python environment. Preview takes a named rule and version, including rules not yet enabled. The optional directory must be new and receives predictions, exact opposing hospital labels, and aggregate counts.

```bash
python scripts/admin/manage_autolabels.py list --dataset CRISP2/LVO
python scripts/admin/manage_autolabels.py preview --dataset CRISP2/LVO --rule crisp2_baseline_timepoint --version 2 --directory ../maintenance/crisp2-preview
python scripts/admin/manage_autolabels.py enable --dataset CRISP2/LVO --rule crisp2_baseline_timepoint --version 2
python scripts/admin/manage_autolabels.py run --dataset CRISP2/LVO
python scripts/admin/manage_autolabels.py export --dataset CRISP2/LVO --file ../maintenance/effective-timepoints.csv
python scripts/admin/manage_autolabels.py export --dataset CRISP2/LVO --conflicts --file ../maintenance/hospital-conflicts.csv
python scripts/admin/manage_autolabels.py disable --dataset CRISP2/LVO --rule crisp2_baseline_timepoint
```

Enable registers the version and computes all visible studies in one transaction. Run refreshes all enabled rules or just the named `--rule`. Disable removes that rule’s registration and results. Export files contain research identifiers. Use new output paths. The CLI reads the configured research database with administrator credentials.

## CRISP2 baseline hospital and recognition rule, version 2

The [executable rule](../../stanford-stroke-pacs/image_ingestion_protocols/autolabels/crisp2.py) refines machine-baseline studies. All other general values pass through, including a missing timepoint. An offset is the DICOM study acquisition timestamp minus the named clinical time. Recognition uses the first parseable value of `time_recognized`, `onset_time`, then `time_normal`. The pre-existing intervention anchor determines general baseline eligibility.

| Condition, in evaluation order | Effective timepoint |
|---|---|
| Before recognition | PRE |
| Recognition unavailable | Continue only within 24 h either side of recorded arrival, inclusive. Otherwise BL. |
| Study at/after recorded puncture, or puncture earlier than arrival | BL |
| No arrival | BLT only with recognition reached, explicit direct presentation (transfer code 0), and no referring match. Otherwise BL. |
| Both hospital imaging clocks match | BL |
| At/after arrival | BL with a referring match. Otherwise BLT before recorded puncture, or through 12 h after arrival inclusive when puncture is unavailable. Beyond 12 h those eligible studies receive FU. |
| From 15 min before arrival to just before arrival | BLT with a receiving match only. Otherwise BL. |
| From 30 min before to more than 15 min before arrival | BL |
| More than 30 min before arrival | BL with a receiving match. Otherwise BLO through 24 h before arrival inclusive. Earlier studies require a referring match for BLO. |

A referring match uses `imaging_refer_bl_time` within 60 minutes, and a receiving match uses `imaging_receiv_bl_time` within 30 minutes. Both tolerances include their endpoints. Missing clocks provide no match. Confirm the meaning of `was_pt_transfer = 0` in the clinical source before enabling this version. The effective label, decision reason, clock sources and minute differences travel together.

The hospital conflict export selects an exact human BLO or BLT with the opposite hospital prediction. Missing references generate no review entries. Preview counts hospital references predicted PRE separately. The arrival-boundary count compares the same exact references with the sign of the arrival offset across all general timepoints. Annotations are evaluation data only.

## Add another rule

Create a module beside `crisp2.py`. The following complete example detects whether any series in a study has the DICOM modality MR. Missing header values leave the result unknown unless an MR series is present.

```python
from .model import Decision, Study, StudyRule


def classify(study: Study) -> Decision:
    modalities = [series.get("Modality") for series in study.series]
    if "MR" in modalities:
        return Decision("yes", "mr_series_present")
    if not modalities or not all(modalities):
        return Decision(None, "missing_series_modality")
    return Decision("no", "all_modalities_known")


HAS_MR = StudyRule(
    name="has_mr",
    version=1,
    output="has_mr",
    clinical_fields=(),
    dicom_fields=("Modality",),
    evaluate=classify,
)
```

Import the declaration into [the registry](../../stanford-stroke-pacs/image_ingestion_protocols/autolabels/__init__.py) and include it in the `RULES` comprehension. Add tests for missing, conflicting, and boundary inputs. Preview the named version on the intended dataset before enabling it. Multiple rules can run together, with one producer per output per dataset. Give changed decision logic a new version and retain the old implementation while any registration uses it.

The runner provides the general timepoint, study clock, declared columns from the enrollment’s registered clinical table, and requested tags from each visible study’s child series ordered by SeriesInstanceUID. Missing clinical columns, rows or cached tags are available as missing values. Evaluate functions perform no I/O. Return a reason and minimal evidence sufficient to explain each decision.

## Persistence and refresh

Alembic revision `0031_dataset_autolabels` creates `dataset_autolabel_rule` and `dataset_autolabel_result`. Join them on dataset and output for the active rule version. Results are keyed by enrollment, study UID and output. Resolve membership through `study_visibility` and scope clinical data to that enrollment’s dataset. A shared study may have different results in different enrollments.

Ingestion and patient linking refresh affected subjects after general timepoints. Clinical import and clear refresh enabled results. The general timepoint recompute and series reclassification commands refresh dependent predictions when applying changes. If an optional rule fails during ingestion, the ingest log records the exception and the general imaging transaction can finish. After correcting the cause, use the dataset run command to refresh the affected predictions. Repeated runs on the same inputs yield identical decisions.
