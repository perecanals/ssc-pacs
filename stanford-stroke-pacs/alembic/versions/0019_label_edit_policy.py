"""label_definitions edit permissions: who may change a label's values

Revision ID: 0019_label_edit_policy
Revises: 0018_drop_femoral_sheath_time
Create Date: 2026-07-16

Adds two columns to ``label_definitions``:

  - ``edit_policy TEXT NOT NULL DEFAULT 'everyone'`` — one of ``everyone`` /
    ``nobody`` / ``users``.
  - ``edit_users TEXT[] NOT NULL DEFAULT '{}'`` — the usernames allowed to edit
    when ``edit_policy = 'users'``; empty for the other two policies.

Together they answer "who may set or clear this label's values":

  ============  =============  =================================================
  edit_policy   edit_users     meaning
  ============  =============  =================================================
  everyone      {}             any authenticated user (the pre-0019 behavior)
  nobody        {}             no one via the API/UI — **including admins**
  users         {a,b,…}        exactly those usernames ("editable by me" is
                               simply ``users = {me}``)
  ============  =============  =================================================

**No admin bypass, deliberately.** ``nobody`` means nobody: the failure mode this
guards against is an admin absent-mindedly clicking a cell and silently
overwriting bulk-loaded clinical data. An admin who genuinely must correct a
value changes the policy, edits, and changes it back — three deliberate steps,
each captured in ``annotations_history``. Changing the policy itself is gated on
being the label's owner (``created_by``) or an admin.

Two columns rather than one sentinel-overloaded array: ``users.allowed_datasets``
gets away with a bare ``text[]`` only because admin-bypass lives in a separate
``is_admin`` column, so ``{}`` unambiguously means deny. Here there are three
states and no companion column, and NULL-vs-empty overloading is exactly the
friction ``dataset_access``'s sentinel comments warn about.

**No backfill on purpose** (same rationale as ``0008_users_allowed_datasets``):
every existing label defaults to ``everyone``, so this revision changes no
behavior on upgrade. Restricting a label is an explicit act via the admin page.
Note the tempting rule "lock the bulk-created labels" would be *wrong* — the
``bulk:%`` set includes ``timepoint`` and ``series_type``, which are
bulk-created but human-maintained (1387 and 360 rater edits respectively when
this shipped). Only ``femoral_sheath_time`` is a pure backfill (0 human edits),
and it is locked from the admin page after deploy, not here.

Both ADDs are nullable-with-default metadata-only operations — instant even on a
populated table. Revision id is 22 chars: ``alembic_version.version_num`` is
``varchar(32)`` and a longer id fails at runtime, not at import.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0019_label_edit_policy"
down_revision: str | Sequence[str] | None = "0018_drop_femoral_sheath_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.label_definitions "
        "ADD COLUMN IF NOT EXISTS edit_policy text NOT NULL DEFAULT 'everyone'"
    )
    op.execute(
        "ALTER TABLE public.label_definitions "
        "ADD COLUMN IF NOT EXISTS edit_users text[] NOT NULL DEFAULT '{}'"
    )
    # Dropped first so a re-run after a partial failure re-adds it cleanly
    # (ADD CONSTRAINT has no IF NOT EXISTS).
    op.execute(
        "ALTER TABLE public.label_definitions "
        "DROP CONSTRAINT IF EXISTS label_definitions_edit_policy_check"
    )
    op.execute(
        "ALTER TABLE public.label_definitions "
        "ADD CONSTRAINT label_definitions_edit_policy_check "
        "CHECK (edit_policy IN ('everyone', 'nobody', 'users'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.label_definitions "
        "DROP CONSTRAINT IF EXISTS label_definitions_edit_policy_check"
    )
    op.execute(
        "ALTER TABLE public.label_definitions DROP COLUMN IF EXISTS edit_users"
    )
    op.execute(
        "ALTER TABLE public.label_definitions DROP COLUMN IF EXISTS edit_policy"
    )
