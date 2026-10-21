"""Admin audit log: `admin_audit_events` (issue P9).

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-21

What an administrator changed in models, prices, aliases, grants, providers and
quota rules: who, when, and before/after for the changed fields. A plain table
with no dialect-specific parts, so it runs on SQLite as it is. The actor is a
foreign key that goes NULL when the account is deleted; the email is copied
into `actor_label` so the row still says who.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_label", sa.String(320), nullable=False, server_default=""),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(80), nullable=True),
        sa.Column("target_label", sa.String(320), nullable=False, server_default=""),
        sa.Column("changes", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_admin_audit_events_created_at", "admin_audit_events", ["created_at"])
    op.create_index(
        "ix_admin_audit_events_actor", "admin_audit_events", ["actor_user_id", "created_at"]
    )
    op.create_index("ix_admin_audit_events_action", "admin_audit_events", ["action", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_audit_events_action", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_actor", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_created_at", table_name="admin_audit_events")
    op.drop_table("admin_audit_events")
