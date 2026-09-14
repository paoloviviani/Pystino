"""Make Presidio recognizer families console-configurable.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-14

Null is deliberately distinct from false: existing append-only engine decisions
continue to use the detector deployment's historical default. New decisions can
independently enable or disable pattern/checksum recognizers and NER.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "redaction_config",
        sa.Column("presidio_pattern_matching", sa.Boolean(), nullable=True),
    )
    op.add_column("redaction_config", sa.Column("presidio_ner", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("redaction_config", "presidio_ner")
    op.drop_column("redaction_config", "presidio_pattern_matching")
