"""self-service password reset: single-use password_reset_tokens

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-02

See docs/adr/0049-password-reset.md.

One row per email sent, and it is spent by use: the hash stored here is what
the confirm endpoint looks up, so the link in the email carries a secret that
never touches the database. Rows are deleted when a newer token is requested
for the same account — a forgotten request does not leave a live door behind —
and cascade with the user, whose erasure must not strand reset rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # SHA-256 of the token, like an API key's (ADR 0010): the token is
        # high-entropy, so a slow KDF would buy nothing over a lookup.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Set at use. A used row is kept until the account's next request
        # replaces it, so "was this link already spent" stays answerable.
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_token_hash", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
