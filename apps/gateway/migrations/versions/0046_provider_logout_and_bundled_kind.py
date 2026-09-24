"""Providers gain a logout URL; the bundled Authelia's row becomes kind authelia.

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-24

`logout_url` (empty by default, which keeps today's behaviour) overrides the
provider's end_session_endpoint for signing out — Authelia 4.39 publishes
none, so signing out used to leave its SSO session alive.

The data step corrects a row the environment seed created before the seed
knew about kinds: every bundled-Authelia install got `kind=generic` with no
sync adapter, which hid the users-file sync and the console's user management
(D10). The bundled Authelia is recognisable without guessing — it is the only
IdP the gateway reaches at http://authelia:9091/authelia — and only a row
still at the old defaults (generic, no adapter) is touched, so an
administrator's own choice is never overwritten and re-running changes
nothing. The adapter starts unconfirmed: nothing is applied before an
administrator has looked at a first dry run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

BUNDLED_AUTHELIA = "http://authelia:9091/authelia"


def upgrade() -> None:
    with op.batch_alter_table("identity_providers") as batch:
        batch.add_column(
            sa.Column("logout_url", sa.String(512), nullable=False, server_default="")
        )
    op.execute(
        sa.text(
            "UPDATE identity_providers "
            "SET kind = 'authelia', sync_adapter = 'authelia_file', sync_confirmed = false "
            "WHERE kind = 'generic' AND sync_adapter = 'none' "
            "AND rtrim(internal_base_url, '/') = :bundled"
        ).bindparams(bundled=BUNDLED_AUTHELIA)
    )


def downgrade() -> None:
    # The kind correction is not undone: a row that is the bundled Authelia
    # was never really generic, and nothing below 0046 reads the difference.
    with op.batch_alter_table("identity_providers") as batch:
        batch.drop_column("logout_url")
