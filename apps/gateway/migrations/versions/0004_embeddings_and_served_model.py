"""embeddings, and recording what actually served a request

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15

See ADR 0028. Three additions, all safe on a
populated table:

* ``models.kind`` — chat or embedding. Everything catalogued today is chat, which
  is what the default backfills.
* ``providers.forward_stream_options`` — whether to inject
  ``stream_options.include_usage``. True preserves today's behaviour exactly.
* ``usage_records.upstream_model`` / ``upstream_provider`` — what the provider
  said served the request — and ``model_substituted``, whether that differed
  from what we asked for. Null and false on existing rows, which is honest: it
  was not recorded at the time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.Enum("chat", "embedding", name="model_kind", native_enum=False, length=32),
                nullable=False,
                server_default="chat",
            )
        )

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "forward_stream_options",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            )
        )

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("upstream_model", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("upstream_provider", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column(
                "model_substituted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("model_substituted")
        batch_op.drop_column("upstream_provider")
        batch_op.drop_column("upstream_model")

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("forward_stream_options")

    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_column("kind")
