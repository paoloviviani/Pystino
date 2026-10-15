"""Group rules gain applies_to: aggregate or each_member (ADR 0098).

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-15

Before this revision every group rule capped the group's aggregate usage.
After it, a rule may cap each member instead. The upgrade must not change
what is in force, so every existing rule becomes ``aggregate`` — the column
is NOT NULL with that server default, which fills existing rows on both
dialects with no separate backfill.

The uniqueness index gains the new column, so a group may hold both an
aggregate rule and a per-member rule for the same metric and window. One
check constraint covers the value set and the scope together: per-member
only means something for a group, and a writer that bypasses the API's 400
still cannot store anything else.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # The constraint needs a table rebuild on SQLite, so the column and the
    # check go through batch mode together; on PostgreSQL these are plain
    # ALTERs. The server default fills existing rows on either dialect.
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "applies_to",
                sa.String(32),
                nullable=False,
                server_default="aggregate",
            )
        )
        batch_op.create_check_constraint(
            "ck_limit_rules_applies_to_group",
            "(applies_to = 'aggregate') OR (applies_to = 'each_member' AND scope = 'group')",
        )
    # The rebuild drops the old table on SQLite, and its expression-based
    # unique index with it (batch cannot reflect expression indexes back);
    # on PostgreSQL the table — and the index — survive the plain ALTERs, so
    # only there is an old index left to drop.
    if _is_postgres():
        op.drop_index("uq_limit_rules_identity", table_name="limit_rules")
    op.create_index(
        "uq_limit_rules_identity",
        "limit_rules",
        [
            "scope",
            sa.text("coalesce(scope_id, '00000000-0000-0000-0000-000000000000')"),
            "metric",
            sa.text("coalesce(window_seconds, -1)"),
            sa.text("coalesce(period, '')"),
            "applies_to",
        ],
        unique=True,
    )


def downgrade() -> None:
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.drop_constraint("ck_limit_rules_applies_to_group", type_="check")
        batch_op.drop_column("applies_to")
    # Same asymmetry as the upgrade, mirrored: only PostgreSQL still holds
    # the six-element index after the rebuild.
    if _is_postgres():
        op.drop_index("uq_limit_rules_identity", table_name="limit_rules")
    op.create_index(
        "uq_limit_rules_identity",
        "limit_rules",
        [
            "scope",
            sa.text("coalesce(scope_id, '00000000-0000-0000-0000-000000000000')"),
            "metric",
            sa.text("coalesce(window_seconds, -1)"),
            sa.text("coalesce(period, '')"),
        ],
        unique=True,
    )
