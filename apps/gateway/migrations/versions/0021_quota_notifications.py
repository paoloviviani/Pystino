"""User-defined quota notifications: settings and dedup state

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-03

See docs/adr/0052-quota-notifications.md.

The split that makes this two tables: ``quota_notification_settings`` is the
user's *decision* — a percentage threshold on a rule that applies to them —
and ``quota_notification_state`` is the machinery's *memory* of what has been
announced, so a threshold fires when the usage rises past it and goes quiet
until the usage falls back below (a reset, or natural decay of a rolling
window). Decisions cascade with their user and their rule; memory does too.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quota_notification_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        # Percentage 1-100, several per user per rule. Arbitrary by request.
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rule_id"], ["limit_rules.id"], ondelete="CASCADE"),
        sa.CheckConstraint("threshold >= 1 AND threshold <= 100", name="ck_quota_threshold_range"),
        sa.PrimaryKeyConstraint("user_id", "rule_id", "threshold"),
    )

    op.create_table(
        "quota_notification_state",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        # The highest usage percentage already announced. A crossing fires when
        # the current percentage reaches a threshold above this; when usage
        # falls below it again the row is reset, which re-arms the threshold.
        sa.Column("notified_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rule_id"], ["limit_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "rule_id"),
    )


def downgrade() -> None:
    op.drop_table("quota_notification_state")
    op.drop_table("quota_notification_settings")
