"""A caller's own label on a usage row.

Revision ID: 0079
Revises: 0078
Create Date: 2026-11-06

``usage_records.tag`` carries the ``x-pystino-tag`` header of a request, so a
caller (a coding agent tagging a session) can ask for its own usage by label.
Nullable: every existing row and every untagged request leaves it null.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tag", sa.String(length=128), nullable=True))
        batch_op.create_index(
            "ix_usage_user_tag_created", ["user_id", "tag", "created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_index("ix_usage_user_tag_created")
        batch_op.drop_column("tag")
