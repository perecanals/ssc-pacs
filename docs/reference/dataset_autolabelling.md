# Automatic study labels

Ingestion assigns general series types, study types, episodes and timepoints. A dataset can enable additional rules by name and version. The browser shows the active `effective_timepoint` for the selected enrollment, including a blank when the rule returns an undetermined result. With no prediction it shows the study’s general timepoint.

## Local rule packages

Place trusted Python packages in `stanford-stroke-pacs/local_rules/<package>/`. Git ignores this directory. Set `SSC_LOCAL_RULES_DIR` to an absolute path to use another directory. Configure the same directory for the web service, ingestion and admin commands. Each immediate subdirectory with an `__init__.py` is loaded at process startup. Restart long-running services after editing packages. Back up this directory with the host configuration, or manage it in a private repository.

Each package exports a sequence named `RULES`. Rule names and versions must be unique across packages. For example, put the following in `local_rules/example/__init__.py`:

```python
from autolabels.model import Decision, StudyRule


def classify(study):
    modalities = [series.get("Modality") for series in study.series]
    if "MR" in modalities:
        return Decision("yes", "mr_series_present")
    if modalities and all(modalities):
        return Decision("no", "all_modalities_known")
    return Decision(None, "missing_modality")


RULES = [
    StudyRule(
        name="has_mr",
        version=1,
        output="has_mr",
        clinical_fields=(),
        dicom_fields=("Modality",),
        evaluate=classify,
    )
]
```

A study evaluator receives the general timepoint, study timestamp, declared fields from the enrollment’s registered clinical table, and requested DICOM tags from its visible study's child series. Missing fields can be read with `.get()`. Return a value, a decision reason and optional JSON evidence. Evaluators run without database I/O.

Use an `EnrollmentRule` to compare studies or rank scans within an episode. Its evaluator receives all visible studies for one enrollment, including owner, episode, study type, acquisition provenance and child series. Return a decision for every study UID. Optional `study_exclusion=(label, value)` matches a trimmed, case-insensitive study annotation. Optional `series_exclusion=(label, substring)` matches a case-insensitive substring in a series annotation. The evaluator receives the exclusion flags. Editing or deleting the declared annotations in the app refreshes the affected enrollments.

Optional rule fields include `reference_label` for the study annotation used in exports, `conflict(value, reference)` for the conflict sheet, and `summarize(cursor, dataset, rule, predictions, references, counts)` for preview metrics. `present(reason, evidence)` returns the user-visible tooltip lines and an estimated-time flag. Include only information appropriate for users with access to that study. An enrollment rule can provide `on_enable(cursor)` to register its annotation vocabulary in the activation transaction.

## Preview, activate and refresh

Run from the stack root in the PACS Python environment. Replace the dataset and rule names with registered values.

```bash
python scripts/admin/manage_autolabels.py list --dataset Example
python scripts/admin/manage_autolabels.py preview --dataset Example --rule has_mr --version 1 --directory ../maintenance/example-preview
python scripts/admin/manage_autolabels.py enable --dataset Example --rule has_mr --version 1
python scripts/admin/manage_autolabels.py run --dataset Example
python scripts/admin/manage_autolabels.py export --dataset Example --file ../maintenance/example-active.csv
python scripts/admin/manage_autolabels.py disable --dataset Example --rule has_mr
```

Preview writes predictions, conflicts and aggregate counts to a new local directory. Enable registers one producer per dataset and output and computes its predictions in the same transaction. Results are keyed by enrollment, study UID and output. Export includes the version, reason, evidence and configured manual reference. Add `--conflicts` to export matching conflicts. Local output files can contain research identifiers.

Ingestion, patient linking, clinical-table changes and classification recomputes refresh the affected predictions. After direct database edits use `run`. Change the version when an evaluator’s decisions change. To restore an earlier version, install its package and enable it. Disable a rule to remove its registration and predictions. If an enabled rule is missing, restore the package and rerun it; the failed refresh preserves stored predictions.

## Clinical anchors for general timepoints

A package can also export `ANCHORS`, a mapping from a strategy name to ordered pairs of clinical column name and offset in hours. For example, `ANCHORS = {"visit": (("procedure_time", 0), ("admission_time", 2))}`. Register the strategy with `manage_datasets.py import-clinical --timepoint-strategy visit`. The columns must exist in the registered clinical table.

General timepoint calculation reads these clocks from a linked subject’s enrollment in the registered dataset. If several registered sources match, the first by dataset name and patient key supplies the row. The first parseable clock in that strategy anchors the nearest episode. Each remaining episode uses its own earliest thrombectomy study. With no clinical strategy, every episode uses the imaging rule.
