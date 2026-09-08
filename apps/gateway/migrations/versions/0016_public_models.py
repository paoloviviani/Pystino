"""public models: the is_public flag

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-30

See ADR 0045.

One column. A model marked public may be used by any authenticated caller,
billed to the caller's own default billing group — access is granted, billing
is not changed: there must still be someone to charge, so a caller with no
billing group is refused exactly as before.

Deliberately a flag on the model rather than a synthetic "public" group. A
group would appear on every user's membership, show up in the console's group
list as a member-bearing row it is not, and make "which models can everyone
use" a join instead of a read. The flag is also what the console toggle edits;
a group would need guarding against deletion and renaming.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("models") as batch:
        batch.add_column(
            sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("(false)"))
        )


def downgrade() -> None:
    with op.batch_alter_table("models") as batch:
        batch.drop_column("is_public")
