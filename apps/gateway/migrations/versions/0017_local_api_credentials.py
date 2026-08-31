"""local `/v1` credentials: refresh_credentials and api_keys.minted_by

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-31

See docs/adr/0046-local-api-credentials.md.

Two pieces, both additive. ``refresh_credentials`` is one row per
``(user, client)``: the long-lived credential a named client exchanges for
access keys. ``api_keys.minted_by`` marks keys the gateway itself minted as
short-lived access credentials, so the listings can hide them and the exchange
can sweep the expired ones without touching a person's own keys.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("client", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=64), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "client", name="uq_refresh_user_client"),
    )
    op.create_index(
        "ix_refresh_credentials_prefix", "refresh_credentials", ["prefix"], unique=True
    )
    op.create_index(
        "ix_refresh_credentials_secret_hash",
        "refresh_credentials",
        ["secret_hash"],
        unique=True,
    )
    op.create_index(
        "ix_refresh_credentials_user_id", "refresh_credentials", ["user_id"]
    )
    op.add_column("api_keys", sa.Column("minted_by", sa.String(length=64), nullable=True))
    op.create_index("ix_api_keys_minted_by", "api_keys", ["minted_by"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_minted_by", table_name="api_keys")
    op.drop_column("api_keys", "minted_by")
    op.drop_index("ix_refresh_credentials_user_id", table_name="refresh_credentials")
    op.drop_index("ix_refresh_credentials_secret_hash", table_name="refresh_credentials")
    op.drop_index("ix_refresh_credentials_prefix", table_name="refresh_credentials")
    op.drop_table("refresh_credentials")
