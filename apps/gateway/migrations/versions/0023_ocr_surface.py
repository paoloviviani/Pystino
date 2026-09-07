"""Per-page pricing, for the OCR surface.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-07

One column: ``model_prices.per_page``, the rate an OCR counterparty charges by.
Nullable, like ``per_image`` beside it, because most models have no such rate
and a zero would read as "pages are free" rather than "this is not priced by the
page".

Two things deliberately *not* in this migration:

* **``models.kind`` gains ``ocr`` with no DDL.** ``_enum`` stores these as
  VARCHAR with no CHECK constraint precisely so that adding a value is not a
  locking change — the docstring on that helper says so, and this is the first
  time it has paid off.
* **No new column on ``usage_records``.** A page charge lands in ``cost`` like
  every other component; ``CostBreakdown`` keeps the parts separate in memory
  for the report, and the ledger records what was billed. Adding a
  ``page_cost`` column would be a second place for the same number to disagree
  with itself.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("per_page", sa.Numeric(24, 12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.drop_column("per_page")
