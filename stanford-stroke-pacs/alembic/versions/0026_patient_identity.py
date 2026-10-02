"""Patient identity: dataset registry, per-dataset enrollments, linked subjects.

`patient_id` alone was the patient identity, so two different people sharing an
id across datasets were merged into one row (`dataset` held both tags). From
here on:

  * `dataset` is a registry: an immutable `slug` (keys, on-disk paths) and a
    mutable display `name` (what users, grants and filters see).
  * A `patient` row is an **enrollment** — one (dataset, patient_id) pair —
    keyed by `patient_key = <slug>__<patient_id>`. `patient.dataset` becomes a
    single name (text, FK to dataset.name, ON UPDATE CASCADE so a rename
    follows).
  * `subject_id` groups the enrollments of one person (default: the row's own
    key). Linked enrollments share imaging; patient labels stay per enrollment.
  * Each study/series is **owned** by one enrollment (`patient_key`) and carries
    its subject (`subject_id`, denormalized); visibility is via the subject.
  * Patient-level annotations are keyed by `patient_key`; the audit trigger
    records it as the history `entity_id`.

Backfill is deliberately generic: a patient tagged with several datasets
becomes one enrollment per dataset, all **linked** (same subject), owned by the
alphabetically first dataset, which also keeps the patient-level annotations.
That is the faithful reading of the old union semantics. Separating rows that
were in fact different people is a data correction, done afterwards with
`scripts/migration/split_merged_patients.py` — not something a schema
migration that also runs on fresh installs should hard-code.

The patient-level annotation backfill runs with the audit trigger disabled: it
is a key rewrite, not an edit, and would otherwise log one fake history row per
annotation. Existing patient-level history `entity_id`s are rewritten to the
new keys once (a documented exception to append-only).

The labelled mirrors are ALTERed in place (never dropped: researcher roles hold
per-table grants on them).

downgrade() restores the old structure only while no patient_id has more than
one enrollment; it refuses otherwise rather than silently re-merging people.
"""

from alembic import op

revision = "0026_patient_identity"
down_revision = "0025_study_modalities"
branch_labels = None
depends_on = None

_SLUGIFY = "btrim(regexp_replace(lower({x}), '[^a-z0-9]+', '-', 'g'), '-')"

# Audit function body with patient-level entity_id = patient_key. Frozen here
# (not shared with 0003) so historical migrations keep their behavior.
_AUDIT_FN = r"""
CREATE OR REPLACE FUNCTION public.annotations_audit() RETURNS TRIGGER AS $$
DECLARE
    _entity_id TEXT;
    _user      TEXT;
BEGIN
    _user := coalesce(nullif(current_setting('app.audit_user', true), ''), 'system');

    IF TG_OP = 'DELETE' THEN
        _entity_id := CASE OLD.level
            WHEN 'patient' THEN OLD.{pkey}
            WHEN 'study'   THEN OLD.studyinstanceuid
            ELSE                OLD.seriesinstanceuid
        END;
        INSERT INTO annotations_history
            (operation, operation_by, annotation_id, level, entity_id, label,
             value_before, notes_before, created_by)
        VALUES
            ('D', _user, OLD.id, OLD.level, _entity_id, OLD.label,
             OLD.value, OLD.notes, OLD.created_by);
        RETURN OLD;

    ELSIF TG_OP = 'UPDATE' THEN
        _entity_id := CASE NEW.level
            WHEN 'patient' THEN NEW.{pkey}
            WHEN 'study'   THEN NEW.studyinstanceuid
            ELSE                NEW.seriesinstanceuid
        END;
        INSERT INTO annotations_history
            (operation, operation_by, annotation_id, level, entity_id, label,
             value_before, value_after, notes_before, notes_after, created_by)
        VALUES
            ('U', _user, NEW.id, NEW.level, _entity_id, NEW.label,
             OLD.value, NEW.value, OLD.notes, NEW.notes, NEW.created_by);
        RETURN NEW;

    ELSIF TG_OP = 'INSERT' THEN
        _entity_id := CASE NEW.level
            WHEN 'patient' THEN NEW.{pkey}
            WHEN 'study'   THEN NEW.studyinstanceuid
            ELSE                NEW.seriesinstanceuid
        END;
        INSERT INTO annotations_history
            (operation, operation_by, annotation_id, level, entity_id, label,
             value_after, notes_after, created_by)
        VALUES
            ('I', _user, NEW.id, NEW.level, _entity_id, NEW.label,
             NEW.value, NEW.notes, NEW.created_by);
        RETURN NEW;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade():
    # --- dataset registry ---------------------------------------------------
    op.execute(
        """
        CREATE TABLE public.dataset (
            slug       text PRIMARY KEY
                       CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
            name       text NOT NULL UNIQUE CHECK (btrim(name) <> ''),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Whoever may read `patient` (researcher / read-only roles, granted per
    # table or via another owner's default privileges) may read the registry
    # its new `dataset` column refers to.
    op.execute(
        """
        DO $$ DECLARE r record; BEGIN
            FOR r IN
                SELECT DISTINCT a.grantee::regrole::text AS grantee
                FROM pg_class c, aclexplode(c.relacl) a
                WHERE c.oid = 'public.patient'::regclass
                  AND a.privilege_type = 'SELECT'
                  AND a.grantee <> 0 AND a.grantee <> c.relowner
            LOOP
                EXECUTE format('GRANT SELECT ON public.dataset TO %s', r.grantee);
            END LOOP;
        END $$
        """
    )
    # Every name in use: patient tags and user grants (a grant may name a
    # dataset nothing has been ingested into yet). Patients that were ingested
    # without a tag get an explicit 'unassigned' dataset.
    op.execute(
        f"""
        INSERT INTO public.dataset (slug, name)
        SELECT {_SLUGIFY.format(x="name")}, name FROM (
            SELECT DISTINCT btrim(d) AS name FROM public.patient, unnest(dataset) d
            UNION
            SELECT DISTINCT btrim(d) FROM public.users, unnest(allowed_datasets) d
        ) n
        WHERE name <> ''
        """
    )
    op.execute(
        """
        INSERT INTO public.dataset (slug, name)
        SELECT 'unassigned', 'unassigned'
        WHERE EXISTS (SELECT 1 FROM public.patient WHERE cardinality(dataset) = 0)
        ON CONFLICT DO NOTHING
        """
    )

    # --- patient → enrollments ----------------------------------------------
    op.execute("ALTER TABLE public.patient DROP CONSTRAINT patient_pkey")
    op.execute("ALTER TABLE public.patient ALTER COLUMN dataset DROP DEFAULT")
    op.execute(
        "ALTER TABLE public.patient "
        "ADD COLUMN patient_key text, ADD COLUMN subject_id text"
    )
    # Owner dataset = alphabetically first tag ('unassigned' when untagged).
    op.execute(
        """
        CREATE TEMP TABLE _owner ON COMMIT DROP AS
        SELECT p.patient_id,
               COALESCE((SELECT min(btrim(d)) FROM unnest(p.dataset) d
                         WHERE btrim(d) <> ''), 'unassigned') AS owner_ds,
               ARRAY(SELECT DISTINCT btrim(d) FROM unnest(p.dataset) d
                     WHERE btrim(d) <> '') AS all_ds
        FROM public.patient p
        """
    )
    # The existing row becomes the owner enrollment...
    op.execute(
        """
        UPDATE public.patient p SET dataset = ARRAY[o.owner_ds]
        FROM _owner o WHERE o.patient_id = p.patient_id
        """
    )
    # ...plus one enrollment per additional tag, sharing the owner's subject.
    op.execute(
        """
        INSERT INTO public.patient
            (patient_id, stroke_date, import_id, import_label, dataset,
             created_at, updated_at)
        SELECT p.patient_id, p.stroke_date, p.import_id, p.import_label,
               ARRAY[d], p.created_at, now()
        FROM public.patient p
        JOIN _owner o USING (patient_id)
        CROSS JOIN LATERAL unnest(o.all_ds) d
        WHERE d <> o.owner_ds
        """
    )
    op.execute(
        "ALTER TABLE public.patient ALTER COLUMN dataset TYPE text "
        "USING btrim(dataset[1])"
    )
    op.execute(
        """
        UPDATE public.patient p
        SET patient_key = d.slug || '__' || p.patient_id,
            subject_id = od.slug || '__' || p.patient_id
        FROM public.dataset d, _owner o, public.dataset od
        WHERE d.name = p.dataset AND o.patient_id = p.patient_id
          AND od.name = o.owner_ds
        """
    )
    op.execute(
        """
        ALTER TABLE public.patient
            ALTER COLUMN patient_key SET NOT NULL,
            ALTER COLUMN subject_id SET NOT NULL,
            ALTER COLUMN dataset SET NOT NULL,
            ADD CONSTRAINT patient_pkey PRIMARY KEY (patient_key),
            ADD CONSTRAINT patient_dataset_patient_id_key UNIQUE (dataset, patient_id),
            ADD CONSTRAINT patient_dataset_fkey FOREIGN KEY (dataset)
                REFERENCES public.dataset (name) ON UPDATE CASCADE
        """
    )
    op.execute("CREATE INDEX idx_patient_subject_id ON public.patient (subject_id)")
    op.execute("CREATE INDEX idx_patient_patient_id ON public.patient (patient_id)")

    # --- study/series ownership ----------------------------------------------
    for table in ("image_study", "image_series"):
        op.execute(
            f"ALTER TABLE public.{table} "
            "ADD COLUMN patient_key text, ADD COLUMN subject_id text"
        )
        # The owner enrollment is the one whose key is its subject.
        op.execute(
            f"""
            UPDATE public.{table} t
            SET patient_key = p.patient_key, subject_id = p.subject_id
            FROM public.patient p
            WHERE p.patient_id = t.patient_id AND p.patient_key = p.subject_id
            """
        )
        op.execute(f"CREATE INDEX idx_{table}_patient_key ON public.{table} (patient_key)")
        op.execute(f"CREATE INDEX idx_{table}_subject_id ON public.{table} (subject_id)")

    # --- patient-level annotations --------------------------------------------
    op.execute("ALTER TABLE public.annotations ADD COLUMN patient_key text")
    op.execute("ALTER TABLE public.annotations DISABLE TRIGGER annotations_audit_trg")
    op.execute(
        """
        UPDATE public.annotations a SET patient_key = p.patient_key
        FROM public.patient p
        WHERE a.level = 'patient' AND p.patient_id = a.patient_id
          AND p.patient_key = p.subject_id
        """
    )
    op.execute("ALTER TABLE public.annotations ENABLE TRIGGER annotations_audit_trg")
    op.execute("DROP INDEX IF EXISTS public.idx_ann_shared_patient")
    op.execute(
        "CREATE UNIQUE INDEX idx_ann_shared_patient ON public.annotations "
        "(patient_key, label) WHERE level = 'patient'"
    )
    op.execute("DROP INDEX IF EXISTS public.idx_annotations_patient")
    op.execute(
        "CREATE INDEX idx_annotations_patient_key ON public.annotations (patient_key)"
    )
    op.execute(
        """
        UPDATE public.annotations_history h SET entity_id = p.patient_key
        FROM public.patient p
        WHERE h.level = 'patient' AND p.patient_id = h.entity_id
          AND p.patient_key = p.subject_id
        """
    )
    op.execute(_AUDIT_FN.replace("{pkey}", "patient_key"))

    # --- labelled mirrors (web-app-created; may not exist yet) ---------------
    op.execute(
        """
        DO $$ BEGIN
            IF to_regclass('public.patient_labelled') IS NOT NULL THEN
                DROP INDEX IF EXISTS public.patient_labelled_patient_id_uidx;
                ALTER TABLE public.patient_labelled ALTER COLUMN dataset TYPE text
                    USING dataset[1];
                ALTER TABLE public.patient_labelled
                    ADD COLUMN IF NOT EXISTS patient_key text,
                    ADD COLUMN IF NOT EXISTS subject_id text;
                -- Existing rows become the owner enrollment's.
                UPDATE public.patient_labelled l
                SET patient_key = p.patient_key, subject_id = p.subject_id,
                    dataset = p.dataset
                FROM public.patient p
                WHERE p.patient_id = l.patient_id AND p.patient_key = p.subject_id;
                DELETE FROM public.patient_labelled WHERE patient_key IS NULL;
                -- The added enrollments carry no patient labels yet.
                INSERT INTO public.patient_labelled
                    (patient_id, stroke_date, import_id, import_label, dataset,
                     created_at, updated_at, patient_key, subject_id)
                SELECT patient_id, stroke_date, import_id, import_label, dataset,
                       created_at, updated_at, patient_key, subject_id
                FROM public.patient p
                WHERE NOT EXISTS (SELECT 1 FROM public.patient_labelled l
                                  WHERE l.patient_key = p.patient_key);
                CREATE UNIQUE INDEX IF NOT EXISTS patient_labelled_patient_key_uidx
                    ON public.patient_labelled (patient_key);
            END IF;
            IF to_regclass('public.image_study_labelled') IS NOT NULL THEN
                ALTER TABLE public.image_study_labelled
                    ADD COLUMN IF NOT EXISTS patient_key text,
                    ADD COLUMN IF NOT EXISTS subject_id text;
                UPDATE public.image_study_labelled l
                SET patient_key = st.patient_key, subject_id = st.subject_id
                FROM public.image_study st
                WHERE st.studyinstanceuid = l.studyinstanceuid;
            END IF;
            IF to_regclass('public.image_series_labelled') IS NOT NULL THEN
                ALTER TABLE public.image_series_labelled
                    ADD COLUMN IF NOT EXISTS patient_key text,
                    ADD COLUMN IF NOT EXISTS subject_id text;
                UPDATE public.image_series_labelled l
                SET patient_key = s.patient_key, subject_id = s.subject_id
                FROM public.image_series s
                WHERE s.seriesinstanceuid = l.seriesinstanceuid;
            END IF;
        END $$
        """
    )


def downgrade():
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.patient
                       GROUP BY patient_id HAVING count(*) > 1) THEN
                RAISE EXCEPTION 'cannot downgrade 0026: some patient_id has more '
                    'than one enrollment; the old schema cannot represent that';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF to_regclass('public.patient_labelled') IS NOT NULL THEN
                DROP INDEX IF EXISTS public.patient_labelled_patient_key_uidx;
                ALTER TABLE public.patient_labelled
                    DROP COLUMN IF EXISTS patient_key,
                    DROP COLUMN IF EXISTS subject_id;
                ALTER TABLE public.patient_labelled ALTER COLUMN dataset TYPE text[]
                    USING ARRAY[dataset];
                CREATE UNIQUE INDEX IF NOT EXISTS patient_labelled_patient_id_uidx
                    ON public.patient_labelled (patient_id);
            END IF;
            IF to_regclass('public.image_study_labelled') IS NOT NULL THEN
                ALTER TABLE public.image_study_labelled
                    DROP COLUMN IF EXISTS patient_key, DROP COLUMN IF EXISTS subject_id;
            END IF;
            IF to_regclass('public.image_series_labelled') IS NOT NULL THEN
                ALTER TABLE public.image_series_labelled
                    DROP COLUMN IF EXISTS patient_key, DROP COLUMN IF EXISTS subject_id;
            END IF;
        END $$
        """
    )
    op.execute(_AUDIT_FN.replace("{pkey}", "patient_id"))
    op.execute(
        """
        UPDATE public.annotations_history h SET entity_id = p.patient_id
        FROM public.patient p
        WHERE h.level = 'patient' AND h.entity_id = p.patient_key
        """
    )
    op.execute("DROP INDEX IF EXISTS public.idx_annotations_patient_key")
    op.execute("CREATE INDEX idx_annotations_patient ON public.annotations (patient_id)")
    op.execute("DROP INDEX IF EXISTS public.idx_ann_shared_patient")
    op.execute(
        "CREATE UNIQUE INDEX idx_ann_shared_patient ON public.annotations "
        "(patient_id, label) WHERE level = 'patient'"
    )
    op.execute("ALTER TABLE public.annotations DROP COLUMN patient_key")
    for table in ("image_study", "image_series"):
        op.execute(
            f"ALTER TABLE public.{table} "
            "DROP COLUMN patient_key, DROP COLUMN subject_id"
        )
    op.execute(
        """
        ALTER TABLE public.patient
            DROP CONSTRAINT patient_dataset_fkey,
            DROP CONSTRAINT patient_dataset_patient_id_key,
            DROP CONSTRAINT patient_pkey
        """
    )
    op.execute("DROP INDEX IF EXISTS public.idx_patient_subject_id")
    op.execute("DROP INDEX IF EXISTS public.idx_patient_patient_id")
    op.execute(
        "ALTER TABLE public.patient ALTER COLUMN dataset TYPE text[] "
        "USING CASE WHEN dataset = 'unassigned' THEN '{}'::text[] ELSE ARRAY[dataset] END"
    )
    op.execute("ALTER TABLE public.patient ALTER COLUMN dataset SET DEFAULT '{}'")
    op.execute(
        "ALTER TABLE public.patient DROP COLUMN patient_key, DROP COLUMN subject_id"
    )
    op.execute("ALTER TABLE public.patient ADD PRIMARY KEY (patient_id)")
    op.execute("DROP TABLE public.dataset")
