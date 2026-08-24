"""provider plugins, and the provider/router distinction

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-22

See docs/adr/0032-provider-plugins.md.

* ``providers.plugin`` — which plugin carries this counterparty's quirks. Null on
  every existing row, which resolves to the generic OpenAI-compatible behaviour:
  exactly what those rows had before plugins existed, so the migration changes no
  behaviour by itself.
* ``providers.kind`` — ``provider`` or ``router``. Defaults to ``provider`` for
  existing rows rather than being guessed from the base URL. A router
  misidentified as a provider records less than it could; one misidentified the
  other way would assert a per-request fact nobody supplied. Under-claiming is
  the safe direction, and an operator can correct it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plugin", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.Enum("provider", "router", name="provider_kind", native_enum=False, length=32),
                nullable=False,
                server_default="provider",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("kind")
        batch_op.drop_column("plugin")
