"""OIDC policy as runtime configuration: oidc_config, append-only

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-02

See docs/adr/0048-oidc-policy-configuration.md.

One table, modelled on ``redaction_config`` (ADR 0033): the identity *policy* —
who may come to exist, which claim names their groups, what an IdP group means
locally, which local group confers admin — becomes a console decision recorded
permanently, while the connection settings (issuer, client secret, redirect)
stay in the environment.

Every policy column is nullable on purpose: a null means "the row says nothing
about this; the environment's value stands", so a row can turn one knob without
having to know the deployment's answer for the rest, and a deployment that
never touches the console behaves exactly as before. The newest row wins.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oidc_config",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # Null = "this row does not decide"; the environment's value stands.
        sa.Column("auto_provision", sa.Boolean(), nullable=True),
        sa.Column("unknown_user_policy", sa.String(length=16), nullable=True),
        sa.Column("groups_claim", sa.String(length=255), nullable=True),
        sa.Column("admin_groups", sa.JSON(), nullable=True),
        # List of [idp_name, local_name] pairs — a JSON array of arrays rather
        # than a side table: the mapping is one decision, saved and read whole,
        # and its history is the row history the table already keeps.
        sa.Column("group_mappings", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "unknown_user_policy IS NULL OR unknown_user_policy IN ('refuse', 'create_inactive')",
            name="ck_oidc_config_unknown_user_policy",
        ),
    )
    op.create_index("ix_oidc_config_created", "oidc_config", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_oidc_config_created", table_name="oidc_config")
    op.drop_table("oidc_config")
