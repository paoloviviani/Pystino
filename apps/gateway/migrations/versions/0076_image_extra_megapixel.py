"""Image models priced by size: a per-extra-megapixel rate.

Revision ID: 0076
Revises: 0075
Create Date: 2026-11-01

Additive only, one nullable column:

* ``model_prices.per_extra_megapixel``: what a counterparty charges for each
  megapixel of an image above the first (Cortecs
  ``output_cost_per_additional_megapixel``), on top of ``per_image``. Nullable
  like the other units: a zero would read as "larger images cost nothing
  extra" rather than "this model is not priced by size".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("per_extra_megapixel", sa.Numeric(24, 12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.drop_column("per_extra_megapixel")
