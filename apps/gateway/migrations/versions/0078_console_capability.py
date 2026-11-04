"""Group capability: console access.

Revision ID: 0078
Revises: 0077
Create Date: 2026-11-04

A group may withhold the gateway's web console from its members, so they use
the chat only and read their quotas there.

* ``groups.console_allowed`` (**existing groups ON**: nothing changes until an
  administrator turns it off; the server default is on for the same reason).
* ``oidc_config.console_mode``: the default new groups are created with
  (``on``/``off``); null defers to ``GATEWAY_ACCESS__CONSOLE`` and then to on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "console_allowed", sa.Boolean(), nullable=False, server_default=sa.text("true")
            )
        )
    with op.batch_alter_table("oidc_config", schema=None) as batch_op:
        batch_op.add_column(sa.Column("console_mode", sa.String(length=16), nullable=True))
        batch_op.create_check_constraint(
            "ck_oidc_config_console_mode",
            "console_mode IS NULL OR console_mode IN ('on', 'off', 'everyone', 'groups')",
        )


def downgrade() -> None:
    with op.batch_alter_table("oidc_config", schema=None) as batch_op:
        batch_op.drop_constraint("ck_oidc_config_console_mode", type_="check")
        batch_op.drop_column("console_mode")
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_column("console_allowed")
