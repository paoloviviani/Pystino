"""model capabilities

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-16

See ADR 0031. Three JSON columns on ``models``,
empty on existing rows — which is honest: nothing knew what those models could
do, and inventing a capability set from the model's name would be a guess
presented as a fact. Re-running discovery fills them in.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("input_modalities", "output_modalities", "supported_features")


def upgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        for name in _COLUMNS:
            batch_op.add_column(
                sa.Column(name, sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
            )

    # Every model we already serve accepts and produces text — that is what
    # being catalogued as a chat or embedding model meant before this existed.
    # The *features* stay empty, because those genuinely were not known.
    op.execute(
        sa.text(
            "UPDATE models SET input_modalities = '[\"text\"]' WHERE kind IN ('chat', 'embedding')"
        )
    )
    op.execute(sa.text("UPDATE models SET output_modalities = '[\"text\"]' WHERE kind = 'chat'"))
    op.execute(
        sa.text("UPDATE models SET output_modalities = '[\"embeddings\"]' WHERE kind = 'embedding'")
    )
    op.execute(sa.text("UPDATE models SET output_modalities = '[\"image\"]' WHERE kind = 'image'"))


def downgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        for name in reversed(_COLUMNS):
            batch_op.drop_column(name)
