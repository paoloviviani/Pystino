"""Routing policies: several targets per alias, a selection rule, stickiness.

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-21

Issue P1, on the table ADR 0101 built for it. `model_route_targets` already was
a list; this adds what a list of more than one needs:

* `model_route_targets.weight`, for the `weighted` rule;
* `models.route_rule` and `models.route_sticky_ttl_seconds`, per alias. The
  defaults (`first_available`, one hour) make every existing alias behave exactly
  as it did: one target, so no rule has anything to choose between;
* `route_sessions`, the session-to-target memory with an expiry column;
* `usage_records.route_attempt`, so a request that fell back leaves rows that
  say which try each was.

Plain additive DDL, so SQLite takes it through `batch_alter_table` and nothing
needs a dialect branch. The downgrade refuses while any alias has more than one
target, for the reason 0063's refuses over alias rows: dropping the columns would
silently collapse a routing decision somebody made, and which target to keep is
not this migration's call.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_route_targets", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("weight", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )

    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "route_rule",
                sa.String(length=32),
                nullable=False,
                server_default="first_available",
            )
        )
        batch_op.add_column(
            sa.Column(
                "route_sticky_ttl_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3600"),
            )
        )

    op.create_table(
        "route_sessions",
        sa.Column("alias_id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("target_model_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["alias_id"], ["models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_model_id"], ["models.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("alias_id", "key_hash"),
    )
    op.create_index("ix_route_sessions_expires", "route_sessions", ["alias_id", "expires_at"])

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("route_attempt", sa.Integer(), nullable=True))


def downgrade() -> None:
    targets = sa.table("model_route_targets", sa.column("alias_id", sa.Uuid()))
    crowded = (
        op.get_bind()
        .execute(
            sa.select(sa.func.count())
            .select_from(
                sa.select(targets.c.alias_id)
                .group_by(targets.c.alias_id)
                .having(sa.func.count() > 1)
                .subquery()
            )
        )
        .scalar_one()
    )
    if crowded:
        raise RuntimeError(
            "0066 downgrade refused: some aliases have more than one route target, and the "
            "schema before it holds exactly one per alias. Reduce each alias to one target "
            "(DELETE FROM model_route_targets for the others) and re-run."
        )

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("route_attempt")

    op.drop_index("ix_route_sessions_expires", table_name="route_sessions")
    op.drop_table("route_sessions")

    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_column("route_sticky_ttl_seconds")
        batch_op.drop_column("route_rule")

    with op.batch_alter_table("model_route_targets", schema=None) as batch_op:
        batch_op.drop_column("weight")
