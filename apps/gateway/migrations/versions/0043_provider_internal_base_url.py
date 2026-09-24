"""An identity provider can be reached server-to-server at an internal URL.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-24

One column on ``identity_providers``: the base URL this gateway uses for
discovery, token, JWKS and userinfo when the issuer's public URL would hairpin
through the deployment's own proxy (the bundled Authelia, reached at
``http://authelia:9091/authelia`` while browsers use ``https://<origin>/authelia``).

It has to be on the row, not only in ``GATEWAY_OIDC__INTERNAL_BASE_URL``: the
environment seeds the first provider once, after which the row is what every
login reads (ADR 0051), so a value only in the environment would be dropped on
the first startup. Empty for every existing row — they keep calling their
issuer exactly as before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("identity_providers") as batch:
        batch.add_column(
            sa.Column(
                "internal_base_url",
                sa.String(length=512),
                nullable=False,
                server_default=sa.text("''"),
            )
        )


def downgrade() -> None:
    """Drop the column: providers go back to calling their public issuer."""
    with op.batch_alter_table("identity_providers") as batch:
        batch.drop_column("internal_base_url")
