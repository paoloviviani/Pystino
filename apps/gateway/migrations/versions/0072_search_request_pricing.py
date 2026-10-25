"""A price per request for web-search backends.

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-26

Additive only. ``search_prices`` is effective-dated and append-only like
``model_prices``, keyed by the backend's plugin name (and an optional tier
override), and ``usage_records.search_price_id`` names the row a search was
billed from.

Nothing is backfilled. A search made before this revision keeps its cost of 0
and a null price reference, and stays that way: repricing history at a price
that did not exist when the search ran would rewrite what was billed. A
backend with no price row bills 0 with no reference, as an unpriced model does,
until an administrator sets one (0 is accepted and means "billed out of band").
The downgrade drops the column and the table; the prices are configuration and
the costs already recorded stay in ``cost``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("tier", sa.String(64), nullable=False, server_default=""),
        sa.Column("per_request", sa.Numeric(24, 12), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        # A plain VARCHAR(32), which is what the ORM's non-native enum is: the
        # values live in `PriceSource`, so a new one never needs a migration.
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "backend", "tier", "effective_from", name="uq_search_prices_backend_tier_effective"
        ),
    )
    op.create_index(
        "ix_search_prices_backend_effective", "search_prices", ["backend", "effective_from"]
    )
    with op.batch_alter_table("usage_records") as batch:
        batch.add_column(sa.Column("search_price_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_usage_records_search_price_id",
            "search_prices",
            ["search_price_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_constraint("fk_usage_records_search_price_id", type_="foreignkey")
        batch.drop_column("search_price_id")
    op.drop_index("ix_search_prices_backend_effective", table_name="search_prices")
    op.drop_table("search_prices")
