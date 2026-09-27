"""identity_providers.link_local_by_email -> link_by_email (ADR 0093 §2).

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-26

A pure rename, no data change: the column was always "does a login here
adopt an existing account by verified email", and that is what
`OIDC_LINK_BY_EMAIL` / `GATEWAY_OIDC__LINK_BY_EMAIL` means now too — the
column just used to be scoped to `issuer="local"`, the only issuer that ever
matched (review correction 1), and now names the thing itself rather than
the door it used to work through.
"""

from __future__ import annotations

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("identity_providers") as batch:
        batch.alter_column("link_local_by_email", new_column_name="link_by_email")


def downgrade() -> None:
    with op.batch_alter_table("identity_providers") as batch:
        batch.alter_column("link_by_email", new_column_name="link_local_by_email")
