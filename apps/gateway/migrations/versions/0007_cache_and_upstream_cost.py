"""cache write tokens, and the provider's own cost figure

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-17

See docs/cache-accounting-findings.md.

* ``usage_records.cache_write_tokens`` — computed since ADR 0030 and never
  stored, so a cached request could not be explained afterwards and the ledger
  could not be repriced. Zero on existing rows, which is honest: for the
  OpenAI-shaped surfaces the count was genuinely not read, and for
  ``/v1/messages`` it is not recoverable now.
* ``usage_records.upstream_cost`` / ``upstream_cost_currency`` — what the
  provider said the request cost. Null on existing rows and on every request to
  a provider that reports nothing or whose unit has not been declared.
* ``providers.upstream_cost_unit`` — how to read that figure. Null everywhere
  after this migration, deliberately: switching it on is a claim about a
  provider's API that somebody has to verify, and defaulting it would have the
  migration make that claim on their behalf.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        # Same precision as `cost`: a per-request figure in micro-units needs
        # room well below the cent, and a float would defeat the point.
        batch_op.add_column(sa.Column("upstream_cost", sa.Numeric(24, 12), nullable=True))
        batch_op.add_column(
            sa.Column("upstream_cost_currency", sa.String(length=3), nullable=True)
        )

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("upstream_cost_unit", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("upstream_cost_unit")

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("upstream_cost_currency")
        batch_op.drop_column("upstream_cost")
        batch_op.drop_column("cache_write_tokens")
