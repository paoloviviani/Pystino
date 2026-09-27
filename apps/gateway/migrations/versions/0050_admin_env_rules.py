"""Admin env rules: users.admin_rule, admin_source oidc -> env, email columns.

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-26

`admin_source='oidc'` becomes `'env'`: both meant "a rule granted this",
and ADR 0093 §5 replaces the per-provider claim rule with one read from
`.env` — the provenance question the column answers has not changed, only
where the rule itself now lives. Every row that migration backfills this way
was granted by the claim mechanism, the only one that existed before, so
`admin_rule` backfills to `'claim'` alongside it.

`email_normalized` and `email_verified` land now, ahead of link-by-email and
merge (stage c), because `OIDC_ADMIN_EMAIL` needs the same normalisation
(ADR 0093 §6.1) to decide anything at all: an admin-email rule that could not
compare a stored, normalised address would only ever act at the moment of a
fresh claim, and never let a startup sweep revoke what the list no longer
lists. `email_verified` has no history to backfill — it stores only the
literal `True` a login actually asserted, and no migration knows what a past
login's provider claimed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("admin_rule", sa.String(8), nullable=True))
        batch.add_column(sa.Column("email_normalized", sa.String(320), nullable=True))
        batch.add_column(sa.Column("email_verified", sa.Boolean(), nullable=True))
    op.create_index("ix_users_email_normalized", "users", ["email_normalized"])

    op.execute(
        sa.text("UPDATE users SET admin_source = 'env', admin_rule = 'claim' "
                "WHERE admin_source = 'oidc'")
    )

    # Imported here rather than at module level (as 0041 did with
    # gateway.bootstrap): a migration must keep loading on a checkout where
    # the application module tree has moved on.
    from gateway.email_normalize import normalize_email

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, email FROM users WHERE email IS NOT NULL")).all()
    for user_id, email in rows:
        connection.execute(
            sa.text("UPDATE users SET email_normalized = :normalized WHERE id = :id"),
            {"normalized": normalize_email(email), "id": user_id},
        )


def downgrade() -> None:
    # admin_source/admin_rule are not reversed: 'env' and 'oidc' meant the
    # same thing at the time this ran, and the claim rule has moved to .env
    # either way — there is nothing a downgrade could restore that upgrading
    # again would not immediately redo.
    op.drop_index("ix_users_email_normalized", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email_verified")
        batch.drop_column("email_normalized")
        batch.drop_column("admin_rule")
