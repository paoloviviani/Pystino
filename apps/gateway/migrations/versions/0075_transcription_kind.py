"""Speech-to-text: a per-minute price and the seconds that were billed.

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-31

Additive only, three nullable columns:

* ``model_prices.per_minute``: the rate a transcription counterparty charges
  by. Nullable, like ``per_page`` and ``per_image``, because a zero would read
  as "audio is free" rather than "this model is not priced by the minute".
* ``usage_records.audio_seconds``: the billable quantity. It is a column, as
  ``image_count`` is and ``page_cost`` deliberately is not: the seconds are what
  a provider's invoice is itemised by and cannot be recovered from the money.
* ``usage_records.audio_seconds_source``: ``upstream`` / ``measured`` /
  ``estimated``, so a report can tell a provider-reported duration from one we
  read off the file or guessed from its size.

``models.kind`` and ``usage_records.api_surface`` gain ``transcription`` with
no DDL: ``_enum`` stores VARCHAR with no CHECK constraint (see 0023).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("per_minute", sa.Numeric(24, 12), nullable=True))
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_seconds", sa.Numeric(14, 3), nullable=True))
        batch_op.add_column(sa.Column("audio_seconds_source", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("audio_seconds_source")
        batch_op.drop_column("audio_seconds")
    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.drop_column("per_minute")
