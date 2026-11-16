"""Add ``models.default_reasoning_effort``.

Revision ID: 0087
Revises: 0086
Create Date: 2026-11-16

The effort the gateway applies to a chat request that names none. Nullable: most
models have no default, and "unset" must stay distinguishable from every effort
a client could send. A plain string, not an enum, because the set of tiers is a
vocabulary the upstreams keep extending (``xhigh``, ``max``) and a column
constraint would need a migration for each.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("models", sa.Column("default_reasoning_effort", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("models") as batch:
        batch.drop_column("default_reasoning_effort")
