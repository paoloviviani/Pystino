"""Per-group capability flags: coding agents and API keys (ADR 0096).

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-13

Two halves, both additive and nullable, with no backfill — the default of
"everyone may hold every capability" is the absence of a decision, so an
upgrade that never touches the console changes nothing.

`groups` gains the two capability flags, defaulting to false. A flag grants
a capability and never withholds one, so a group with nothing set is an
ordinary chat group; the effective capability for a person folds the
deployment setting over these (see `gateway.capabilities`).

`oidc_config` gains the two deployment settings: `code_agents_mode` and
`api_keys_mode`, each `everyone` or `groups`. Like every other column on
that table, null defers to the environment (`GATEWAY_ACCESS__CODE_AGENTS`
and `GATEWAY_ACCESS__API_KEYS`), the newest row wins, and the API validates
first — the check constraints mirror `ck_oidc_config_join_mode` from 0057 as
the second line of defence, and the fold logs-and-defers on a hand-mangled
row rather than crashing every login.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("groups") as batch:
        batch.add_column(
            sa.Column(
                "code_agents_allowed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )
        batch.add_column(
            sa.Column(
                "api_keys_allowed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )
    with op.batch_alter_table("oidc_config") as batch:
        batch.add_column(sa.Column("code_agents_mode", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("api_keys_mode", sa.String(length=16), nullable=True))
        batch.create_check_constraint(
            "ck_oidc_config_code_agents_mode",
            "code_agents_mode IS NULL OR code_agents_mode IN ('everyone', 'groups')",
        )
        batch.create_check_constraint(
            "ck_oidc_config_api_keys_mode",
            "api_keys_mode IS NULL OR api_keys_mode IN ('everyone', 'groups')",
        )


def downgrade() -> None:
    with op.batch_alter_table("oidc_config") as batch:
        batch.drop_constraint("ck_oidc_config_api_keys_mode", type_="check")
        batch.drop_constraint("ck_oidc_config_code_agents_mode", type_="check")
        batch.drop_column("api_keys_mode")
        batch.drop_column("code_agents_mode")
    with op.batch_alter_table("groups") as batch:
        batch.drop_column("api_keys_allowed")
        batch.drop_column("code_agents_allowed")
