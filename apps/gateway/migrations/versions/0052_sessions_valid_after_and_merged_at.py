"""users.sessions_valid_after and users.merged_at (ADR 0093 §3.1, §9.1).

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-26

`sessions_valid_after` is the disable cascade's own clock (stage b, §9.1): a
disable stamps it to `now()`, and it is what makes disabling someone mean
"and every session they already hold, too" rather than "starting from their
next request that happens to notice `is_active`". `load_user_for_management`
compares it against the *session token's own* `iat` — already on the token,
carried through `verify_session_token` from this point on — so refusing a
stale console session costs no extra query per request, the same budget
`test_query_counts.py` already holds the login path to.

`merged_at` is stage c's (the merge). It lands now, beside its sibling, so
`/v1/me` (already returning both — `schemas.CallerIdentity`, stage a) has
real columns to read instead of the two literal `None`s it fills in until
this migration: nothing downstream needs a second migration to start working
once stage c writes the column.

Both are nullable with no default: a user nothing has ever revoked or merged
into is `NULL` on both, which is the common case forever, not a value this
migration would need to backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("sessions_valid_after", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("merged_at")
        batch.drop_column("sessions_valid_after")
