"""Identity providers gain a kind, group/admin authority and sync settings.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-24

ADR 0088 (draft). Every column has a server default that reproduces today's
behaviour exactly — kind generic, groups from the claim, admin decided in the
console, subject from `sub`, no sync adapter — so the upgrade changes nothing
until an administrator changes a provider. `users` gains provenance for the
admin flag (existing admins were all made in the console or by the CLI, so
"manual" is true for every existing row) and for deactivation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_PROVIDER_COLUMNS = (
    sa.Column("kind", sa.String(32), nullable=False, server_default="generic"),
    sa.Column("group_source", sa.String(16), nullable=False, server_default="claim"),
    sa.Column("admin_source", sa.String(16), nullable=False, server_default="console"),
    sa.Column("admin_claim", sa.String(255), nullable=False, server_default="groups"),
    sa.Column("admin_values", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    sa.Column("subject_claim", sa.String(64), nullable=False, server_default="sub"),
    sa.Column("sync_adapter", sa.String(32), nullable=False, server_default="none"),
    sa.Column("sync_config_encrypted", sa.Text(), nullable=True),
    sa.Column("sync_interval_minutes", sa.Integer(), nullable=False, server_default=sa.text("60")),
    sa.Column("sync_deprovision", sa.String(16), nullable=False, server_default="disable"),
    sa.Column("sync_create_users", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    sa.Column("sync_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
)


def upgrade() -> None:
    with op.batch_alter_table("identity_providers") as batch:
        for column in _PROVIDER_COLUMNS:
            batch.add_column(column.copy())
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("admin_source", sa.String(16), nullable=False, server_default="manual")
        )
        batch.add_column(sa.Column("deactivated_by", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("deactivated_by")
        batch.drop_column("admin_source")
    with op.batch_alter_table("identity_providers") as batch:
        for column in reversed(_PROVIDER_COLUMNS):
            batch.drop_column(column.name)
