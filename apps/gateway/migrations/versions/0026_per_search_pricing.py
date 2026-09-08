"""Per-search pricing, for provider-side web search.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-08

Two columns (ADR 0058).

``model_prices.per_search`` is the rate, per **one** search — nullable, like
``per_image`` and ``per_page`` beside it, because most models have no such rate
and a zero would read as "searches are free" rather than "this is not priced by
the search". Every provider publishes the figure per thousand ($10 per 1,000);
the division belongs where the number is entered, not in the arithmetic that
multiplies it by a count of searches.

``usage_records.search_count`` is the billable count. A column for exactly the
reason ``image_count`` is one — and it is worth saying why this does *not*
contradict migration 0023, which refused a ``page_cost`` column. That refusal
was about **cost**: a second place for a number already implied by ``cost``,
free to disagree with it. This is a **count**, it is the unit a provider's
invoice is itemised by, and it cannot be recovered from the money afterwards —
least of all when the model carried no rate and the money is therefore zero.

Both default to nothing charged and nothing counted, so an upgrade changes no
figure. What it changes is that a search on a priced model starts being billed;
a deployment where callers already pass the tool will see its ledger begin to
include a charge it was previously missing, which is the point.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("per_search", sa.Numeric(24, 12), nullable=True))

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("search_count")
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.drop_column("per_search")
