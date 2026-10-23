"""Local connectors: a capability of its own (Cerea ADR 0102).

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-23

A connectors-only machine (galopin serving a person's own MCP servers to the
chat) signs in with the same machine OIDC client as an agent machine, so until
now the ``code_agents`` gate decided it. It gets its own per-group flag,
managed exactly like ``code_agents`` and ``api_keys``.

`groups` gains ``local_connectors_allowed``. **Existing groups are set ON.**
The feature is new and opt-out per group ("new groups default on"), and the
alternative — leaving every existing group off — would ship a capability that
nobody has until an administrator visits every group, which is the opposite of
the default new groups get. Before this revision a connectors machine did not
exist, so no behaviour in force is widened by it: the flag only lets a person
run MCP servers on their own computer, and it grants no model access (the
machine client still reaches only ``/v1/me`` without ``code_agents``). An
administrator who wants it off switches it off per group or for all groups in
the console.

`oidc_config` gains ``local_connectors_mode`` — the default new groups are
created with (``on``/``off``), null deferring to
``GATEWAY_ACCESS__LOCAL_CONNECTORS`` and then to ``on``. No carry-forward row
is needed: null already means "on".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("groups") as batch:
        batch.add_column(
            sa.Column(
                "local_connectors_allowed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )
    op.get_bind().execute(sa.text("UPDATE groups SET local_connectors_allowed = :on"), {"on": True})
    with op.batch_alter_table("oidc_config") as batch:
        batch.add_column(sa.Column("local_connectors_mode", sa.String(length=16), nullable=True))
        batch.create_check_constraint(
            "ck_oidc_config_local_connectors_mode",
            "local_connectors_mode IS NULL OR local_connectors_mode IN "
            "('on', 'off', 'everyone', 'groups')",
        )


def downgrade() -> None:
    with op.batch_alter_table("oidc_config") as batch:
        batch.drop_constraint("ck_oidc_config_local_connectors_mode", type_="check")
        batch.drop_column("local_connectors_mode")
    with op.batch_alter_table("groups") as batch:
        batch.drop_column("local_connectors_allowed")
