"""chat_erasures (ADR 0093 §9.2, §9.3).

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-26

``gateway_user_id`` carries no foreign key, on purpose and for the same
reason `user_merges`' columns don't: the row this names is deleted in the
same transaction that inserts this one, so the column's job is to tell the
chat who to erase, not to reference a user row that, by the time anything
reads this table, no longer exists here at all.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_erasures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("gateway_user_id", sa.Uuid(), nullable=False),
        sa.Column("identities", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "status",
            sa.Enum("pending", "done", name="chat_erasure_status", native_enum=False, length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chat_counts", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_erasures_gateway_user_id", "chat_erasures", ["gateway_user_id"])
    # The retry loop's own query: due rows, oldest first.
    op.create_index(
        "ix_chat_erasures_pending_due",
        "chat_erasures",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_erasures_pending_due", table_name="chat_erasures")
    op.drop_index("ix_chat_erasures_gateway_user_id", table_name="chat_erasures")
    op.drop_table("chat_erasures")
