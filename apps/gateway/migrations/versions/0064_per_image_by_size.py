"""Image prices by size: `model_prices.per_image_by_size`.

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-21

Every image usage row already records the size it was made at, and nothing
could price by it, so a 1024x1024 and a 1792x1024 picture cost the same. A
price row may now carry a rate per size (JSON, amounts as strings: JSON has no
exact decimal). Null, the default, keeps today's single `per_image` rate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("per_image_by_size", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.drop_column("per_image_by_size")
