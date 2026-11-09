"""Per-detector switches on the append-only redaction config rows.

Revision ID: 0084
Revises: 0083
Create Date: 2026-11-09

The Detectors tab switches each detector on or off: the pattern recognisers
(``presidio_pattern_matching``, already there), and each NER engine, spaCy and
GLiNER. Null is "not decided": the row's older ``presidio_ner`` (or the
deployment default) applies, so existing rows behave exactly as before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("redaction_config", sa.Column("spacy_enabled", sa.Boolean(), nullable=True))
    op.add_column("redaction_config", sa.Column("gliner_enabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("redaction_config") as batch:
        batch.drop_column("gliner_enabled")
        batch.drop_column("spacy_enabled")
