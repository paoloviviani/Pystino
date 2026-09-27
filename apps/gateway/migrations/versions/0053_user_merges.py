"""user_merges (ADR 0093 §3.1, §7.1).

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-26

The schema lands with stage c's announce door, ahead of the merge endpoints
themselves (§7.1): `GET /v1/me/identities` (`schemas.MeIdentities`) already
answers `merged_from`, resolved by walking this table, and an empty table
until the merge lands is exactly the right answer for a deployment where
nobody has been merged yet.

`source_user_id` and `target_user_id` carry no foreign key, deliberately, the
same choice `identity_events` makes: a merge deletes the source row in the
same transaction this is written in, and the target may itself be merged
into someone else later. Both ids must go on naming who they named at the
time, not cascade away or dangle a constraint against a row this table's own
purpose is to outlive.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_merges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_merges_source_user_id", "user_merges", ["source_user_id"])
    op.create_index("ix_user_merges_target_user_id", "user_merges", ["target_user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_merges_target_user_id", table_name="user_merges")
    op.drop_index("ix_user_merges_source_user_id", table_name="user_merges")
    op.drop_table("user_merges")
