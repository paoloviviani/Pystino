"""the redaction engine becomes an admin decision, on the record

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-24

See docs/adr/0033-redaction-engine-selection.md.

``redaction_config`` is append-only: one row per change, newest wins, nothing
updated and nothing deleted. Switching redaction off decides whether personal
data leaves this deployment, and the question a data-protection review asks is
about a window that has already closed — "who turned it off, when, and why" — so
the history *is* the feature. Same shape as ``quota_resets`` and ``model_prices``
for the same reason.

The table starts **empty**, deliberately. No row means the environment decides,
so a deployment that never opens the console behaves exactly as it did before
this migration. Seeding it with the current ``GATEWAY_REDACTION__ENGINE`` would
have looked tidier and would have been wrong: it would silently pin today's
environment value into the database, so a later change to the environment would
stop taking effect with nothing to explain why.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "redaction_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("engine", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        # SET NULL rather than CASCADE: erasing a user must not erase the record
        # of what they changed about personal-data handling.
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_redaction_config_created", "redaction_config", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_redaction_config_created", table_name="redaction_config")
    op.drop_table("redaction_config")
