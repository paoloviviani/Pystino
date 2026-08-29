"""local authentication: the local_credentials table

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-29

See docs/adr/0043-local-authentication.md.

One table, and deliberately nothing on ``users``. A user's identity is
``(issuer, subject)`` and most rows will never carry a password; "who can sign
in locally" is then a query over this table rather than a scan for a non-null
column, and revoking local login is a row deletion rather than an UPDATE that
leaves the empty string behind.

Nothing is backfilled. The user ``gateway seed`` creates has always been
``issuer="local"``, but it has never had a credential — it authenticates with
its API key. Giving it a password without anyone choosing one would put a
guessable account on the management surface; an operator who wants one runs
``gateway passwd``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_credentials",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("local_credentials")
