"""Append-only identity_events (ADR 0093 §3.1), closing review R8.

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-26

The redesign's audit trail lands first, ahead of every action it will end up
recording, because most of those actions do not exist yet in this stage —
they arrive across later migrations and stages, and each one writes through
`gateway.identity_events.record_event` from the day it lands rather than
being retrofitted with logging afterwards.

"Append-only" is a database guarantee, not an agreement among the routes that
touch the table, unlike `oidc_config` and `directory_sync_runs`, which stay
insert-only only because nothing calls `.update()` on them. For an audit
table that is not enough — a route added carelessly two years from now must
fail loudly the first time it tries an `UPDATE`, in every environment,
without anyone needing to have reviewed it for that. Hence the trigger below,
on both dialects this gateway runs on. Its SQL is imported from
`gateway.models` rather than repeated here: the unit suite never runs this
migration (`tests/conftest.py` builds its schema from
`Base.metadata.create_all()`), so a copy that only lived here would leave the
trigger the tests exercise silently different from the one a deployment gets.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from gateway.models import (
    IDENTITY_EVENTS_POSTGRES_FUNCTION_SQL,
    IDENTITY_EVENTS_POSTGRES_TRIGGER_SQL,
    IDENTITY_EVENTS_SQLITE_NO_DELETE_TRIGGER_SQL,
    IDENTITY_EVENTS_SQLITE_NO_UPDATE_TRIGGER_SQL,
)

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "identity_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_label", sa.String(320), nullable=False, server_default=""),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_label", sa.String(320), nullable=False, server_default=""),
        sa.Column("issuer", sa.String(512), nullable=True),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_identity_events_actor_user", "identity_events", ["actor_user_id", "at"])
    op.create_index("ix_identity_events_target_user", "identity_events", ["target_user_id", "at"])
    op.create_index("ix_identity_events_action", "identity_events", ["action", "at"])

    if _is_postgres():
        op.execute(IDENTITY_EVENTS_POSTGRES_FUNCTION_SQL)
        op.execute(IDENTITY_EVENTS_POSTGRES_TRIGGER_SQL)
    else:
        op.execute(IDENTITY_EVENTS_SQLITE_NO_UPDATE_TRIGGER_SQL)
        op.execute(IDENTITY_EVENTS_SQLITE_NO_DELETE_TRIGGER_SQL)


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP TRIGGER IF EXISTS identity_events_append_only ON identity_events")
        op.execute("DROP FUNCTION IF EXISTS identity_events_append_only()")
    else:
        op.execute("DROP TRIGGER IF EXISTS identity_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS identity_events_no_delete")

    op.drop_index("ix_identity_events_action", table_name="identity_events")
    op.drop_index("ix_identity_events_target_user", table_name="identity_events")
    op.drop_index("ix_identity_events_actor_user", table_name="identity_events")
    op.drop_table("identity_events")
