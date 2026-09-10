"""The directory's own name for a person: ``users.username``.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-09

One nullable column, from a bug report: *"I don't see chat@local in the
console"*.

The account was there. It was listed as `chat@example.org`, because that is
what the gateway stored — and `chat@local` was the Keycloak **username**, which
`sync_user_from_claims` read only as a *fallback* for the display name
(``claims.get("name") or claims.get("preferred_username")``). Keycloak sends
both, so the username was discarded on every login, and an operator could not
find an account by the name they had created it under.

`display_name` and `username` answer different questions and that is why this
is a column rather than a change to the existing one. `display_name` is what to
show a person. `username` is what they are *called* in the directory an
administrator created them in, and it is the string that administrator types
into a search box.

**Nullable, and backfilled by nothing.** The gateway cannot know a username it
was never sent: the claim arrives with a login, so each account fills its own
in on the next one. A migration that guessed — copying `display_name`, or
splitting an email — would write a value nobody's directory agrees with, and
searching for it would then find the wrong person rather than nobody. An empty
column that fills itself is the honest state.

Not unique, and not identity. Identity remains ``(issuer, subject)``: a
username is mutable, and two directories may legitimately use the same one for
two different people. Indexed because it is searched, not to constrain it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("username", sa.String(255), nullable=True))
    op.create_index("ix_users_username", "users", ["username"])


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("username")
