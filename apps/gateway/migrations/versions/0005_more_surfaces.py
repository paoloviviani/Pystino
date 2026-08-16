"""responses, anthropic messages and image generation

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-16

See docs/adr/0030-more-surfaces.md. Four additions, all safe on a populated
table:

* ``models.kind`` gains ``image``. The column is a non-native enum (a varchar
  with a check constraint), so widening it means rewriting the constraint —
  which ``batch_alter_table`` does by rebuilding the table on SQLite and with a
  plain DROP/ADD on Postgres.
* ``model_prices.per_image`` — for the image models nobody prices per token.
* ``usage_records.api_surface`` — which endpoint served the request. Existing
  rows are backfilled from the model's kind, which is exactly right for them:
  before this migration there were only two surfaces and each had one kind.
* ``usage_records.image_count`` / ``image_size`` — zero and null on existing
  rows, which is honest.
* ``providers.auth_scheme`` — bearer or ``x-api-key``. Defaults to bearer,
  which is what every existing provider uses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SURFACES = ("chat_completions", "embeddings", "responses", "messages", "images")


def upgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(length=32),
            type_=sa.Enum(
                "chat", "embedding", "image", name="model_kind", native_enum=False, length=32
            ),
            existing_nullable=False,
            existing_server_default="chat",
        )

    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("per_image", sa.Numeric(24, 12), nullable=True))

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auth_scheme",
                sa.Enum("bearer", "x_api_key", name="auth_scheme", native_enum=False, length=32),
                nullable=False,
                server_default="bearer",
            )
        )

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "api_surface",
                sa.Enum(*_SURFACES, name="api_surface", native_enum=False, length=32),
                nullable=False,
                server_default="chat_completions",
            )
        )
        batch_op.add_column(
            sa.Column("image_count", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(sa.Column("image_size", sa.String(length=32), nullable=True))

    # Backfill: every historical row went through one of the two surfaces that
    # existed, and which one is decided by the model's kind. Rows whose model
    # was deleted keep the default, which is the commoner of the two.
    op.execute(
        sa.text(
            "UPDATE usage_records SET api_surface = 'embeddings' "
            "WHERE model_id IN (SELECT id FROM models WHERE kind = 'embedding')"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("image_size")
        batch_op.drop_column("image_count")
        batch_op.drop_column("api_surface")

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("auth_scheme")

    with op.batch_alter_table("model_prices", schema=None) as batch_op:
        batch_op.drop_column("per_image")

    # Any model catalogued as an image model becomes a chat model on the way
    # down; the alternative is refusing to downgrade, and a stuck migration is
    # worse than a mislabelled row an operator can see and fix.
    op.execute(sa.text("UPDATE models SET kind = 'chat' WHERE kind = 'image'"))
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(length=32),
            type_=sa.Enum("chat", "embedding", name="model_kind", native_enum=False, length=32),
            existing_nullable=False,
            existing_server_default="chat",
        )
