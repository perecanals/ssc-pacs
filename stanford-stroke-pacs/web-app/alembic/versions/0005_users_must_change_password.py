"""users.must_change_password: force first-login + admin reset rotations

Revision ID: 0005_users_must_change_password
Revises: 0004_label_def_instrument
Create Date: 2026-05-14

Adds two columns to ``users``:

 - ``must_change_password BOOLEAN NOT NULL DEFAULT FALSE`` — when TRUE, the
   user is required to set a new password before they can use the API. The
   change-password endpoint flips it back to FALSE; admin-driven resets via
   ``scripts/admin/manage_users.py`` flip it back to TRUE.
 - ``password_changed_at TIMESTAMPTZ`` — set by the change-password endpoint
   when the user picks their own password. NULL means "never self-chosen".

Existing users are flagged TRUE so everyone rotates on their next login.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0005_users_must_change_password"
down_revision: str | Sequence[str] | None = "0004_label_def_instrument"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ"
    )
    op.execute("UPDATE public.users SET must_change_password = TRUE")


def downgrade() -> None:
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS password_changed_at")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS must_change_password")
