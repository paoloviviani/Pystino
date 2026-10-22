"""Keycloak is no longer a provider kind or a sync adapter.

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-22

ADR 0088's bundled-Keycloak story is gone: Keycloak is now just an external
OIDC provider, configured like any other. The ``keycloak`` kind and the
``keycloak_admin`` pull adapter are withdrawn; SCIM, the Authelia file adapter
and generic OIDC are unchanged.

A deployment that ran the old adapter has rows that name it, so they are
moved rather than left dangling: ``kind`` becomes ``generic`` and a
``keycloak_admin`` adapter becomes ``none``, with its stored service-account
credentials and review state cleared (the secret is useless without the
adapter). A provider whose groups came from that mirror
(``group_source = directory``) moves to ``none``, because ``directory`` with
no adapter is a policy the console refuses to save: memberships stay as they
are and are managed in the console from then on, rather than switching to the
token's claim, which could empty them at the next login. Users already
created by earlier pulls keep their accounts and (issuer, subject) links; only
the scheduled mirror stops. The downgrade is a
no-op: the old values cannot be recovered, and ``generic`` is valid on the
older schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = sa.table(
        "identity_providers",
        sa.column("kind", sa.String),
        sa.column("sync_adapter", sa.String),
        sa.column("group_source", sa.String),
        sa.column("sync_config_encrypted", sa.Text),
        sa.column("sync_confirmed", sa.Boolean),
    )
    op.execute(
        table.update()
        .where(table.c.sync_adapter == "keycloak_admin", table.c.group_source == "directory")
        .values(group_source="none")
    )
    op.execute(
        table.update()
        .where(table.c.sync_adapter == "keycloak_admin")
        .values(sync_adapter="none", sync_config_encrypted=None, sync_confirmed=False)
    )
    op.execute(table.update().where(table.c.kind == "keycloak").values(kind="generic"))


def downgrade() -> None:
    pass
