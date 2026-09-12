"""The vector store belongs to the chat.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-12

ADR 0070. The knowledge pipeline leaves the gateway: files, bases, documents,
chunks and the pipeline configuration were application data the gateway kept
on the chat's behalf, and the chat now keeps them itself. What the gateway
keeps is what the pipeline consumes — the embedding endpoint and the
document-reading service — both untouched by this migration.

``resource_shares`` rows of every remaining kind are deleted before their
kinds disappear: the only kind left after this one ran is none. Chat projects
were shared in the chat's own store already, so nothing readable is lost here;
the table itself goes, because an ACL with no resource behind it is exactly
the "row pointing at nothing" joins exist to prevent.

Downgrade refuses. The chunks and documents are in the chat's store now; a
downgrade that recreated empty tables would claim a capability it cannot
honour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_TABLES = [
    "knowledge_chunks",
    "knowledge_documents",
    "knowledge_bases",
    "knowledge_config",
    "file_blobs",
    "files",
    "resource_shares",
]


def upgrade() -> None:
    # Children before parents: chunks name documents and bases, documents name
    # files and bases, blobs back files.
    for table in _TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def downgrade() -> None:
    raise RuntimeError(
        "ADR 0070: the knowledge pipeline moved to the chat. The tables this "
        "migration drops cannot be rebuilt from here — the documents and "
        "vectors live in the chat's store now."
    )
