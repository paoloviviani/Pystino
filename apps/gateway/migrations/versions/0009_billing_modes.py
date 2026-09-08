"""two billing modes, and both figures recorded in either

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-22

See ADR 0032, decision 6.

* ``providers.billing_mode`` — ``own_prices`` (the default, and what every row
  did before this existed) or ``provider_reported`` for a deployment whose policy
  is strict pass-through cost recovery.
* ``usage_records.computed_cost`` — our arithmetic, recorded whichever figure was
  billed. Backfilled from ``cost`` on existing rows, which is exactly right:
  every historical row was billed from our own prices, so the two were the same
  number.
* ``usage_records.cost_source`` — which figure produced ``cost``. Defaults to
  ``own_prices`` for the same reason.
* ``usage_records.upstream_cost_details`` — the counterparty's breakdown,
  verbatim. Null on existing rows; it was never captured.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES = ("own_prices", "provider_reported", "own_prices_fallback")


def upgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "billing_mode",
                sa.Enum(
                    "own_prices",
                    "provider_reported",
                    name="billing_mode",
                    native_enum=False,
                    length=32,
                ),
                nullable=False,
                server_default="own_prices",
            )
        )

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("computed_cost", sa.Numeric(24, 12), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column(
                "cost_source",
                sa.Enum(*_SOURCES, name="cost_source", native_enum=False, length=32),
                nullable=False,
                server_default="own_prices",
            )
        )
        batch_op.add_column(sa.Column("upstream_cost_details", sa.JSON(), nullable=True))

    # Every existing row was billed from our own prices, so the figure we
    # computed *is* the figure we charged. Copying it keeps the invariant that
    # `computed_cost` is always populated, rather than leaving a zero that would
    # read as "we calculated nothing".
    op.execute(sa.text("UPDATE usage_records SET computed_cost = cost"))


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("upstream_cost_details")
        batch_op.drop_column("cost_source")
        batch_op.drop_column("computed_cost")

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("billing_mode")
