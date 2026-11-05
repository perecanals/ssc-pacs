"""Evaluation references must not turn unlabeled or ambiguous series into negatives."""

import importlib.util
from pathlib import Path

EVALUATOR = (
    Path(__file__).resolve().parents[2]
    / "scripts/data_integrity/evaluate_series_types.py"
)
spec = importlib.util.spec_from_file_location("series_evaluation", EVALUATOR)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_only_clear_manual_families_are_scored():
    assert evaluation.manual_family("NCCT2") == "NCCT"
    assert evaluation.manual_family("CTA3") == "CTA"
    assert evaluation.manual_family("MRP") == "PWI"
    assert evaluation.manual_family("Pre") == "DSA"
    assert evaluation.manual_family("NCCTdontuse") is None
    assert evaluation.manual_family("wrong label") is None
    assert evaluation.manual_family(None) is None


def test_unlabeled_predictions_are_unknown_not_false_positives():
    def row(uid, manual):
        return dict(
            seriesinstanceuid=uid,
            subject_id=uid,
            patient_key=uid,
            dataset="test",
            manual=manual,
            tags={"Modality": "CT", "SeriesDescription": "CTA"},
            same_position_count=1,
            n_instances_scanned=200,
            series_type="NCCT",
        )

    metrics, errors = evaluation.evaluate(
        [
            row("a", "CTA"),
            row("b", None),
            row("c", "NCCTdontuse"),
            row("d", "NCCT"),
        ]
    )
    cta = metrics["groups"]["all"]["CTA"]
    assert cta["recall_on_labeled"] == 1
    assert cta["precision_among_labeled"] == 0.5
    assert cta["baseline_correct"] == 0
    assert metrics["unlabeled_predictions"] == {"CTA": 1}
    assert metrics["excluded_manual_labels"] == {"NCCTdontuse": 1}
    assert len(errors) == 1
    assert errors[0]["seriesinstanceuid"] == "d"
