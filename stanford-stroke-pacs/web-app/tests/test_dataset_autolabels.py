from pathlib import Path


def test_shared_study_and_refresh(db_conn, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "image_ingestion_protocols"))
    from autolabels import RULES
    from autolabels.crisp2 import BASELINE_TIMEPOINT
    from autolabels.model import Decision, StudyRule
    from autolabels.runner import refresh

    extra = StudyRule(
        "dicom_test", 1, "series_count", (), lambda s: Decision(str(len(s.series)), "count"), ("Modality",)
    )
    monkeypatch.setitem(RULES, (extra.name, extra.version), extra)
    with db_conn.cursor() as cur:
        cur.execute("ALTER TABLE lvo_clinical_data ADD COLUMN receiving_arrival_time timestamp")
        cur.execute("UPDATE lvo_clinical_data SET receiving_arrival_time='2025-02-02 01:00'")
        for dataset, rule in (("lvo", BASELINE_TIMEPOINT), ("crisp2", BASELINE_TIMEPOINT), ("lvo", extra)):
            cur.execute(
                "INSERT INTO dataset_autolabel_rule VALUES (%s,%s,%s,%s)",
                (dataset, rule.output, rule.name, rule.version),
            )
        refresh(cur)
        cur.execute("""SELECT dataset,output,value FROM dataset_autolabel_result
            WHERE studyinstanceuid='1.2.3.4.5' ORDER BY dataset,output""")
        assert cur.fetchall() == [
            ("crisp2", "effective_timepoint", "BL"),
            ("lvo", "effective_timepoint", "BLO"),
            ("lvo", "series_count", "1"),
        ]
        cur.execute("UPDATE lvo_clinical_data SET receiving_arrival_time=NULL")
        cur.execute("DELETE FROM study_member WHERE patient_key='crisp2__P-0001'")
        refresh(cur, subjects=["lvo__P-0001"])
        refresh(cur)
        cur.execute("""SELECT dataset,value FROM dataset_autolabel_result
            WHERE studyinstanceuid='1.2.3.4.5' AND output='effective_timepoint'""")
        assert cur.fetchall() == [("lvo", "BL")]


def test_admin_cli_round_trip(seeded_db, monkeypatch, tmp_path):
    import csv
    import sys

    import psycopg2

    scripts = Path(__file__).resolve().parents[2] / "scripts" / "admin"
    monkeypatch.syspath_prepend(str(scripts))
    import manage_autolabels as cli

    monkeypatch.setattr(cli, "get_conn", lambda: psycopg2.connect(**seeded_db))
    rule = ["--rule", "crisp2_baseline_timepoint"]

    def run(command, *args):
        monkeypatch.setattr(sys, "argv", ["manage_autolabels", command, "--dataset", "lvo", *args])
        cli.main()

    preview = tmp_path / "preview"
    run("preview", *rule, "--version", "1", "--directory", str(preview))
    try:
        run("enable", *rule, "--version", "1")
        run("run")
        output = tmp_path / "persisted.csv"
        run("export", "--file", str(output))
        with output.open() as f, (preview / "predictions.csv").open() as g:
            actual = {(r["patient_key"], r["studyinstanceuid"]): r["value"] for r in csv.DictReader(f)}
            expected = {(r["patient_key"], r["studyinstanceuid"]): r["effective_timepoint"] for r in csv.DictReader(g)}
        assert actual == expected
        assert actual
    finally:
        run("disable", *rule)
    run("export", "--file", str(tmp_path / "disabled.csv"))
    with (tmp_path / "disabled.csv").open() as f:
        assert list(csv.DictReader(f)) == []
