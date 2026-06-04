"""Add annotations_history table with audit trigger

Revision ID: 0003_annotations_history
Revises: 0002_warming_started_at
Create Date: 2026-04-15

Adds an append-only ``annotations_history`` table that captures every
INSERT, UPDATE, and DELETE on ``annotations`` via a PL/pgSQL trigger.

The trigger reads ``current_setting('app.audit_user', true)`` to
attribute each change to the authenticated user.  The web app
middleware sets this session variable at the start of each request's
transaction via ``SET LOCAL``.

See workstream 12 (annotation audit trail) and
``documentation/operations/annotation_history.md``.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0003_annotations_history"
down_revision: str = "0002_warming_started_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_SQL = r"""
-- =========================================================================
-- annotations_history: append-only audit trail for annotations
-- =========================================================================

CREATE TABLE public.annotations_history (
    history_id       BIGSERIAL PRIMARY KEY,
    operation        CHAR(1) NOT NULL,           -- I = insert, U = update, D = delete
    operation_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation_by     TEXT NOT NULL DEFAULT 'system',
    annotation_id    INTEGER NOT NULL,
    level            TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    label            TEXT NOT NULL,
    value_before     TEXT,                        -- NULL on INSERT
    value_after      TEXT,                        -- NULL on DELETE
    notes_before     TEXT,                        -- NULL on INSERT
    notes_after      TEXT,                        -- NULL on DELETE
    created_by       TEXT                         -- snapshot of annotations.created_by
);

CREATE INDEX annotations_history_annotation_id_idx
    ON public.annotations_history (annotation_id, operation_at DESC);

CREATE INDEX annotations_history_entity_id_idx
    ON public.annotations_history (entity_id, operation_at DESC);

-- =========================================================================
-- PL/pgSQL trigger function
-- =========================================================================

CREATE OR REPLACE FUNCTION public.annotations_audit() RETURNS TRIGGER AS $$
DECLARE
    _entity_id TEXT;
    _user      TEXT;
BEGIN
    _user := coalesce(nullif(current_setting('app.audit_user', true), ''), 'system');

    IF TG_OP = 'DELETE' THEN
        _entity_id := CASE OLD.level
            WHEN 'patient' THEN OLD.patient_id
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
            WHEN 'patient' THEN NEW.patient_id
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
            WHEN 'patient' THEN NEW.patient_id
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

-- =========================================================================
-- Attach the trigger
-- =========================================================================

CREATE TRIGGER annotations_audit_trg
    AFTER INSERT OR UPDATE OR DELETE ON public.annotations
    FOR EACH ROW EXECUTE FUNCTION public.annotations_audit();
"""


DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS annotations_audit_trg ON public.annotations;
DROP FUNCTION IF EXISTS public.annotations_audit();
DROP TABLE IF EXISTS public.annotations_history;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
