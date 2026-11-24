"""Opt-in study rules and enrollment-scoped predictions."""

from alembic import op

revision = "0031_dataset_autolabels"
down_revision = "0030_sr_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE dataset_autolabel_rule (
        dataset text REFERENCES dataset(name) ON UPDATE CASCADE ON DELETE CASCADE,
        output text, rule text NOT NULL, version integer NOT NULL,
        PRIMARY KEY(dataset, output)
    )""")
    op.execute("""CREATE TABLE dataset_autolabel_result (
        dataset text NOT NULL, output text NOT NULL,
        patient_key text REFERENCES patient(patient_key) ON UPDATE CASCADE ON DELETE CASCADE,
        studyinstanceuid text REFERENCES image_study(studyinstanceuid) ON DELETE CASCADE,
        value text, reason text NOT NULL, evidence jsonb NOT NULL,
        PRIMARY KEY(patient_key, studyinstanceuid, output),
        FOREIGN KEY(dataset, output) REFERENCES dataset_autolabel_rule(dataset, output)
        ON UPDATE CASCADE ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX dataset_autolabel_scope ON dataset_autolabel_result(dataset,output)")


def downgrade():
    op.drop_table("dataset_autolabel_result")
    op.drop_table("dataset_autolabel_rule")
