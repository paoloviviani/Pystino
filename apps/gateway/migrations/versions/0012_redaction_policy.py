"""per-entity redaction policy

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-26

See docs/adr/0037-redaction-policy.md.

One nullable JSON column on the append-only ``redaction_config`` table, rather
than a table of entity rows. A policy is a document an admin edits and saves
whole; it is read once per poll and never queried across; and half a policy is
not a weaker policy, it is a different one — which is exactly what a partial
insert into a row-per-entity table would leave behind.

Existing rows keep ``NULL``, and null is not "redact nothing". It means the row
decided only about the engine, so the deployment's own default policy applies —
the same rule as "no row means the environment decides", one level down. That is
what keeps this migration silent for a deployment that has already switched
engines from the console.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("redaction_config", sa.Column("policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("redaction_config", "policy")
