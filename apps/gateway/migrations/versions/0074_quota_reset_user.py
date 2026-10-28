"""A quota reset can be about one user.

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-28

Additive only. ``quota_resets.user_id`` is null for a reset of the whole rule
(every existing row, unchanged) and names one user for a reset of that user's
consumption of a per-user rule. The foreign key is ON DELETE CASCADE rather
than SET NULL: a nulled user would turn the row into a whole-rule reset and
zero everyone's counter, so an erased user's resets are deleted with them.

The downgrade drops the per-user rows first. Left in place without their
column they would read as whole-rule resets and reset everyone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("quota_resets") as batch:
        batch.add_column(sa.Column("user_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_quota_resets_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
    op.create_index("ix_quota_resets_rule_user", "quota_resets", ["rule_id", "user_id"])


def downgrade() -> None:
    op.execute("DELETE FROM quota_resets WHERE user_id IS NOT NULL")
    op.drop_index("ix_quota_resets_rule_user", table_name="quota_resets")
    with op.batch_alter_table("quota_resets") as batch:
        batch.drop_constraint("fk_quota_resets_user_id", type_="foreignkey")
        batch.drop_column("user_id")
