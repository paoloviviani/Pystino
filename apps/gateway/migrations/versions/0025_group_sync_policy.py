"""Who granted a membership, and how far a directory's answer reaches.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-07

Two columns (ADR 0057).

``memberships.source`` records whether a login granted a membership or an
administrator did, because only the first is the directory's to take away.
The default is ``manual``: four of the five places that create a membership are
administrative, exactly one is the login sync, and a row that does not know
where it came from is safer treated as somebody's decision here than as a
directory's to revoke.

**The back-fill is the interesting part.** Existing rows carry no provenance,
so it is inferred from the only evidence there is: a membership belonging to a
user whose ``issuer`` is not ``local`` was made by a login sync, because until
now that sync replaced every membership of such a user on every login — an
administrator's grant could not survive long enough to be in this table.
A membership of a ``local`` user was made by hand, since no sync ever runs for
one. That inference is exact for the behaviour being replaced, which is what
makes the upgrade invisible: revocation keeps working for directory users, and
local accounts keep their groups.

``identity_providers.group_sync`` defaults to ``every_login``, which is what
this gateway has always done, so no configured provider changes behaviour by
being upgraded.

Both columns are VARCHAR rather than native enums: ``_enum`` in ``models.py``
exists so that adding a value later is not a locking DDL change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.Enum(
                    "oidc",
                    "manual",
                    name="membership_source",
                    native_enum=False,
                    length=32,
                ),
                nullable=False,
                server_default=sa.text("'manual'"),
            )
        )

    # Everything a directory granted, named as such. See the module docstring:
    # this is exact for the behaviour it replaces, not a guess.
    op.execute(
        """
        UPDATE memberships
           SET source = 'oidc'
         WHERE user_id IN (SELECT id FROM users WHERE issuer <> 'local')
        """
    )

    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "group_sync",
                sa.Enum(
                    "every_login",
                    "first_login",
                    "never",
                    name="group_sync",
                    native_enum=False,
                    length=32,
                ),
                nullable=False,
                server_default=sa.text("'every_login'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.drop_column("group_sync")
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.drop_column("source")
