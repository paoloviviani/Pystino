"""A provider row may name the catalogue entry it is an instance of.

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-22

``providers.catalogue_id`` links a row to an entry of the YAML provider
catalogue (backlog P4). It is a plain nullable string, not a foreign key: the
catalogue is files read at startup, so there is no table to point at, and an id
the catalogue later loses (an operator removed their own entry) must leave the
row working. Existing rows stay null: guessing a link from the plugin name would
attach region and retention claims nobody made. An administrator links a row in
the console.

Nothing is copied from the catalogue into the row, so there is nothing to
backfill and nothing to go stale. The downgrade drops the column; the link is
metadata, and the rows lose nothing they could not be re-linked to.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("catalogue_id", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("providers") as batch:
        batch.drop_column("catalogue_id")
