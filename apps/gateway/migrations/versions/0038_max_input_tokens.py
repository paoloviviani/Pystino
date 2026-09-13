"""Add ``models.max_input_tokens``.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-13

The "in" half of the context limit, set by hand: catalogue imports and the
community price file say nothing about a provider's real input cap, so this
column has no automatic source and is never filled by a refresh. Nullable
because "unknown" is the honest state — a prompt that exceeds it is rejected
only when the operator has recorded a limit at all.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("models", sa.Column("max_input_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("models", "max_input_tokens")
