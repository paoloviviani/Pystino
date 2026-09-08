"""Exchange rates for pricing that is not in the billing currency.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-03

See ADR 0054.

Two pieces:

* ``fx_rates`` — one row per currency pair, holding the last known rate and
  when it was fetched. ADR 0054's fallback ("rely on the last known if no
  answer") needs this to survive restarts: a Valkey-only cache would turn a
  cache flush into an outage for every USD-priced model.
* ``usage_records`` gains the **native** figure alongside the billing one:
  ``cost_native``/``cost_currency`` are what the counterparty's price table
  produced (USD, for everything imported from the new catalogues), and
  ``cost_fx_rate`` is the rate that produced ``cost`` — so a per-provider or
  per-model breakdown can report USD, and a converted aggregate can be
  reconstructed and audited later. Nullable everywhere: EUR-priced models
  have no conversion to record.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("base", sa.String(length=3), nullable=False),
        sa.Column("quote", sa.String(length=3), nullable=False),
        # The rate as published: quote units per one base unit. Ten decimal
        # places, because a rate is used in arithmetic that lands in money
        # columns and the rounding should happen once, visibly, at the end.
        sa.Column("rate", sa.Numeric(18, 10), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fx_rates_base_quote", "fx_rates", ["base", "quote"], unique=True)

    op.add_column(
        "usage_records",
        sa.Column("cost_native", sa.Numeric(24, 12), nullable=True),
    )
    op.add_column(
        "usage_records", sa.Column("cost_currency", sa.String(length=3), nullable=True)
    )
    op.add_column(
        "usage_records", sa.Column("cost_fx_rate", sa.Numeric(18, 10), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("usage_records", "cost_fx_rate")
    op.drop_column("usage_records", "cost_currency")
    op.drop_column("usage_records", "cost_native")
    op.drop_index("ix_fx_rates_base_quote", table_name="fx_rates")
    op.drop_table("fx_rates")
