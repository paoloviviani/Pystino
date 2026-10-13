"""Join modes and invitations (ADR 0095).

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-12

Two halves, both nullable with no backfill:

`oidc_config` gains the join-mode columns. `join_mode` names one of the four
answers to "who may join" (`open`, `approval`, `invite`, `group`);
`required_groups` is the JSON list of local group names `group` mode admits;
`group_fallback` is what a stranger without one gets (`refuse`, or an
inactive account via `approval`). The check constraints mirror the existing
`ck_oidc_config_unknown_user_policy` from 0018: the API validates first, and
the fold logs-and-defers on a hand-mangled row rather than crashing every
login, so the constraints are the second line of defence, not the first.

`users` gains the invitation stamp: `invited_at` when the invite was
(over)issued, `invited_by` who issued it. An invite older than 14 days
behaves as no invite under the join gate; re-invite re-stamps `invited_at`.
`ON DELETE SET NULL`: erasing the issuer must not erase the invitee.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("oidc_config") as batch:
        batch.add_column(sa.Column("join_mode", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("required_groups", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("group_fallback", sa.String(length=16), nullable=True))
        batch.create_check_constraint(
            "ck_oidc_config_join_mode",
            "join_mode IS NULL OR join_mode IN ('open', 'approval', 'invite', 'group')",
        )
        batch.create_check_constraint(
            "ck_oidc_config_group_fallback",
            "group_fallback IS NULL OR group_fallback IN ('refuse', 'approval')",
        )
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("invited_by", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_users_invited_by",
            "users",
            ["invited_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_invited_by", type_="foreignkey")
        batch.drop_column("invited_by")
        batch.drop_column("invited_at")
    with op.batch_alter_table("oidc_config") as batch:
        batch.drop_constraint("ck_oidc_config_group_fallback", type_="check")
        batch.drop_constraint("ck_oidc_config_join_mode", type_="check")
        batch.drop_column("group_fallback")
        batch.drop_column("required_groups")
        batch.drop_column("join_mode")
