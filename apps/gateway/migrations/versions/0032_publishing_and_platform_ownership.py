"""Shared with everyone, and owned by the platform.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-11

Two independent things, ADR 0066. Publishing is a *grant* — the publisher keeps
ownership — and platform ownership is an *absence of owner*, so a corpus does
not leave when the administrator who built it does.

**No DDL for the third principal kind.** ``resource_shares.principal_kind`` is
an ``_enum`` column, which in this schema is ``VARCHAR`` with no ``CHECK``
(migration 0023 recorded the property; 0031 relied on it). ``everyone`` is
therefore a value the existing column already accepts, and there is nothing to
alter. The ``principal_id`` of such a row is the nil UUID: the column is part
of the composite primary key and cannot be null, ``uuid4`` cannot produce the
nil UUID, and ``resource_shares`` has no foreign keys, so nothing has to
pretend that id addresses a row.

**The owner columns become nullable, and the foreign key stays CASCADE.** That
second half is the load-bearing part and the reason this migration is worth
reading. ``ON DELETE SET NULL`` is the natural spelling once a column becomes
nullable, and here it would be a data-protection bug: erasing a user would
silently promote their private corpus into a platform resource that
administrators can read, at the exact moment that person exercised their right
to be forgotten. Left as ``CASCADE``, null is only ever written deliberately —
so the column has exactly one meaning and "ownerless" can never be confused
with "erased". Erasing a user still takes their bases and agents with them.

**Files are untouched.** ``files.owner_user_id`` stays ``NOT NULL``: sharing
happens on the base so that "who can read this document" has one answer, and
an ownerless file would be a second one.

Downgrade refuses rather than guessing. A platform resource has no owner to
restore, so making the column ``NOT NULL`` again would either fail on real data
or require inventing an owner for somebody else's corpus; and an ``everyone``
grant would silently become a row whose ``principal_id`` names nothing. Both
are worse than a migration that stops and says what is in the way.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("knowledge_bases", "agents"):
        op.alter_column(
            table,
            "owner_user_id",
            existing_type=sa.Uuid(),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("knowledge_bases", "agents"):
        orphans = bind.execute(
            sa.text(f"select count(*) from {table} where owner_user_id is null")  # noqa: S608
        ).scalar_one()
        if orphans:
            raise RuntimeError(
                f"{orphans} row(s) in {table} are owned by the deployment and have no owner "
                "to restore. Transfer or delete them before downgrading; inventing an owner "
                "for somebody else's corpus is not something this migration will do."
            )
    published = bind.execute(
        sa.text("select count(*) from resource_shares where principal_kind = 'everyone'")
    ).scalar_one()
    if published:
        raise RuntimeError(
            f"{published} resource(s) are published to everyone. Downgrading would leave "
            "grants whose principal_id names nothing. Unpublish them first."
        )
    for table in ("knowledge_bases", "agents"):
        op.alter_column(
            table,
            "owner_user_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
