"""Profile fields an administrator edited stop following the directory.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-15

One nullable-by-default JSON column on ``users``: the subset of ``email`` /
``display_name`` / ``username`` an administrator has set from the console.

The problem it closes was found reading the login path end to end before
wiring the console's new profile editors: ``oidc.provision_user`` refreshed
all three fields from the token's claims on *every* sign-in (the local door
never touches them — it only stamps ``last_login_at``). An administrator's
correction to a directory account therefore reverted silently at that
person's next login, and the ``username`` variant was worse: the
backfill clause in ``sync_user_from_claims`` treats a stored username that
differs from the token as a divergence, so the reverted value would have been
re-written on the next ``/v1`` request — the edit never survived even to the
next login.

Rather than stop syncing these fields outright — a directory rename would then
never arrive, and the backfill the ``username`` clause exists for would die —
the column records *which* fields the console has claimed. Provisioning skips
exactly those; everything else keeps following the token. The same settlement
ADR 0057 reached for group memberships (an administrator's grant survives a
sign-in; the directory still owns what it granted), applied to the profile.

The default is ``'[]'`` server-side, so every existing row reads as
"nothing claimed" and current behaviour is unchanged until an administrator
actually edits someone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "admin_edited_fields", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
            )
        )


def downgrade() -> None:
    """Drop the column: unrecorded edits resume reverting at the next login.

    Nothing else to undo — the column carries no state an operator set outside
    the console edit it exists to remember.
    """
    with op.batch_alter_table("users") as batch:
        batch.drop_column("admin_edited_fields")
