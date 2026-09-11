"""Metering our own web searches: a count, two labels, and no price.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-11

Three columns on ``usage_records``, for the search backends of phase 2
(docs/web-search-plan.md): Exa, Jina, Staan, Linkup.

**Why a count and not a cost.** Every other billable unit in this ledger has a
rate beside it. This one does not, deliberately. Two of the four vendors'
prices cannot be established at source: Jina publishes no per-token figure
publicly at all, and Staan's higher "for AI" tier is neither a documented
request parameter nor reported back in the response. A ``per_search``-shaped
rate here would therefore be a guess in half the rows, wearing the same type as
a measurement — which is the failure ``own_prices_fallback`` exists to make
loud one column over. A request count is never wrong; it is also the unit a
vendor's dashboard itemises, so it reconciles with no currency, no rounding
rule and no rate table to drift.

**Why ``own_search_requests`` is not ``search_count``.** That column already
exists and means something else: server-side searches the *counterparty* ran
inside a chat request and charged us for (ADR 0058). Adding to it would give
one number two meanings, and no report could then tell "Anthropic searched"
from "we called Staan" — the same class of mistake ``cost`` /
``computed_cost`` / ``upstream_cost`` exist to prevent.

**Why the two label columns land now, unpriced.** ``own_search_backend`` and
``own_search_tier`` carry no rate and nothing reads them to decide anything.
They are here because they cannot be added afterwards: which vendor served a
search, and at which depth, is a fact only that request knows, and a later
migration would find nothing to backfill it from. Reconciling a vendor's
invoice against rows that do not say which vendor they went to is not possible
at any price — and if a money layer is ever wanted, the tier is what a rate
would have to be looked up by (Exa ``deep-reasoning`` is $15 per 1,000 against
``instant`` at $7; Linkup ``deep`` is ten times ``flash``).

``limit_rules.metric`` gains ``own_search_requests`` and needs **no DDL**:
``_enum`` stores these as VARCHAR with no CHECK, precisely so that a new metric
is not a locking change. That is also what makes this migration safe to apply
before any search backend exists — nothing writes a non-zero count yet, so no
figure moves, and an operator who creates such a rule today gets a ceiling that
is simply never approached.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "own_search_requests",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        # Nullable, and backfilled by nothing: a row written before this
        # migration ran no search of ours, so there is no backend to name.
        # Guessing one would put a vendor's name on a request that never
        # reached it, which is worse than an empty column.
        batch_op.add_column(sa.Column("own_search_backend", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("own_search_tier", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("own_search_tier")
        batch_op.drop_column("own_search_backend")
        batch_op.drop_column("own_search_requests")
