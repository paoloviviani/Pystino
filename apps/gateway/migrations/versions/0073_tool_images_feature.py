"""``tool_images``: a model that can be shown images returned by a tool.

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-28

The chat's ``look_doc`` tool hands a vision model the pages of a document as
images, after a tool result. Some vision models, or the chat templates they are
served with, ignore or drop images placed there, so whether a model can use them
is a per-model capability and belongs beside the others: a value in the open
``supported_features`` set, editable in the console.

Data only, no schema change. Every model whose ``input_modalities`` include
``image`` gains ``tool_images``; a model that does not accept images is not
touched. The default is on because most vision models do handle it and an
operator should not have to visit every model before the feature works; the
ones that do not are switched off in the console.

Idempotent: a model that already lists the feature is left as it is, so running
the revision twice (or after an operator had already added it) writes nothing.
It runs once per database, which is also what keeps an operator's later removal
from being undone. The downgrade removes the value from every model, which is
the state the schema had before the value existed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

FEATURE = "tool_images"

# Named columns rather than the ORM class: a migration describes its own SQL.
# The id is untyped on purpose: it is only carried from the select to the update,
# so the driver's own value goes back unchanged. Declaring it a UUID would parse
# it, and fail on a row whose id is not a well-formed UUID (older test fixtures
# write ids like "m1"), for no benefit.
_models = sa.table(
    "models",
    sa.column("id"),
    sa.column("input_modalities", sa.JSON()),
    sa.column("supported_features", sa.JSON()),
)


def upgrade() -> None:
    conn = op.get_bind()
    # Read in Python rather than with a JSON operator: SQLite and PostgreSQL
    # spell "array contains" differently, and a catalogue is a few hundred rows.
    for model_id, inputs, features in conn.execute(
        sa.select(_models.c.id, _models.c.input_modalities, _models.c.supported_features)
    ).all():
        if "image" not in (inputs or []):
            continue
        current = list(features or [])
        if FEATURE in current:
            continue
        conn.execute(
            sa.update(_models)
            .where(_models.c.id == model_id)
            .values(supported_features=sorted({*current, FEATURE}))
        )


def downgrade() -> None:
    conn = op.get_bind()
    for model_id, features in conn.execute(
        sa.select(_models.c.id, _models.c.supported_features)
    ).all():
        current = list(features or [])
        if FEATURE not in current:
            continue
        conn.execute(
            sa.update(_models)
            .where(_models.c.id == model_id)
            .values(supported_features=[item for item in current if item != FEATURE])
        )
