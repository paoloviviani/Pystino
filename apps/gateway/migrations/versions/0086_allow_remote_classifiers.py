"""A deployment setting: may a privacy classifier use a model outside the deployment.

Revision ID: 0086
Revises: 0085
Create Date: 2026-11-09

Classifying sends the text to the classifier's model, so by default only models
that run inside the deployment may be used. ``allow_remote_classifiers`` is off
until an administrator turns it on. A classifier already stored with a remote
model is kept and, while the setting is off, cannot answer (it fails closed).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "privacy_settings",
        sa.Column(
            "allow_remote_classifiers",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("privacy_settings") as batch:
        batch.drop_column("allow_remote_classifiers")
