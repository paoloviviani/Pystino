"""seen_groups: directory group names recorded rather than created.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-08

`GATEWAY_OIDC__GROUP_IMPORT=manual` (the new default) stops a login from
creating a group for every name its token carries. A real GitLab sign-in
created 67 that way. The names are recorded here instead, per issuer, and the
console imports or dismisses them. `seen_group_users` says who carried each
name at their last provisioning: the count the console shows, and the people
an import grants the new group to at once.

`users.unresolved_group_names` is the per-person half that keeps the `/v1`
hot path write-free: the bearer path compares a token with the row it already
loaded, and a name that did not resolve last time is not a reason to
provision again. Nullable, with no backfill: NULL reads as "nothing recorded",
which makes an existing user's first request after the upgrade provision once
(if their token names a group they do not hold) and settle.

`users.default_group_granted_at` stamps the one-time grant of the default
group (`GATEWAY_OIDC__DEFAULT_GROUP`, `users` by default). NULL for everyone
here, deliberately: the next sign-in of each existing person is the backfill,
and from then on an administrator's removal sticks.

Nothing is dropped: `auto_create_groups` was a setting, not a column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seen_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "name", name="uq_seen_groups_issuer_name"),
    )
    op.create_index("ix_seen_groups_name", "seen_groups", ["name"])
    op.create_table(
        "seen_group_users",
        sa.Column("seen_group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["seen_group_id"], ["seen_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("seen_group_id", "user_id"),
    )
    op.create_index("ix_seen_group_users_user_id", "seen_group_users", ["user_id"])
    # A plain ADD COLUMN of a nullable column: no batch mode needed on SQLite.
    op.add_column("users", sa.Column("unresolved_group_names", sa.JSON(), nullable=True))
    op.add_column(
        "users", sa.Column("default_group_granted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("default_group_granted_at")
        batch.drop_column("unresolved_group_names")
    op.drop_index("ix_seen_group_users_user_id", table_name="seen_group_users")
    op.drop_table("seen_group_users")
    op.drop_index("ix_seen_groups_name", table_name="seen_groups")
    op.drop_table("seen_groups")
