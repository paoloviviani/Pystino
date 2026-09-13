"""A provider can prefix the model names it contributes.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-13

Two vendors whose tiers share a word — Linkup and Exa both sell a "deep" —
collide in one catalogue, and the operator's fix was to type the vendor into
each name by hand, at import, and hope to remember it for the next dozen.
The prefix is that habit, on the provider where it belongs.

Empty string for every existing row: a prefix is a decision, and a migration
that minted one from a provider's name would rename models people are
already calling — with no way to know the operator wanted it. Adding the
prefix afterwards re-applies it to the models already here (the update route
renames), so the column starting empty costs nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "providers",
        sa.Column("prefix", sa.String(32), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("providers", "prefix")
