"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text  # noqa: F401  (autogenerate emits it inside JSONB)
from sqlalchemy.dialects import postgresql  # noqa: F401  (JSON().with_variant)

# The two imports above are unused in most migrations and are here anyway.
# Autogenerate renders a `JSON().with_variant(JSONB(), "postgresql")` column as
# `sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")`
# and imports neither name, so the migration fails at runtime with a NameError
# rather than at generation time — in the container, on a fresh database, after
# a clean build.

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
