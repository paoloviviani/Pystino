"""The house issuer's single-use authorization codes.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-11

ADR 0068. The one piece of the IdP that cannot be stateless: an authorization
code must be single-use, and single-use is a server-side fact. A signed,
self-contained code would keep working until its TTL no matter what this
table thought, so the code is stored here — hashed (the ADR 0010 argument: a
2^256-entropy value has nothing for a slow KDF to defend), bound to its
client, redirect URI and PKCE challenge, and dead the moment ``used_at`` is
set.

Rows are ephemeral by design — sixty seconds, one spend — so the table holds
at most a small residue of unspent codes from logins that never completed.
The token endpoint's sweep of expired access keys is the pattern; nothing
needs a cleanup job here, because nothing reads this table but the flow that
wrote it.

Downgrade drops the table: codes are worthless without the issuer that minted
them, and no state worth preserving can live in a sixty-second row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idp_authorization_codes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("client_id", sa.String(64), nullable=False, index=True),
        sa.Column("redirect_uri", sa.String(512), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("nonce", sa.String(128), nullable=False, server_default=""),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("idp_authorization_codes")
