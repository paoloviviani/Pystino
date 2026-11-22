"""Ledger indexes for a quota rule limited to a provider or a model.

Revision ID: 0090
Revises: 0089
Create Date: 2026-11-22

``ix_usage_model_created`` and ``ix_usage_provider_created`` serve the ledger reads of a
filtered rule (seeding, rebuilding, the database fallback). They were split out of 0089 so
that a failed build leaves 0089 applied and a re-run retries only these two.

On PostgreSQL they are built CONCURRENTLY, outside a transaction, so building them does not
block writes to the ledger. A failed or cancelled CONCURRENTLY build leaves an INVALID index
behind, and ``CREATE INDEX IF NOT EXISTS`` would then skip it and keep the broken index; so an
invalid leftover is dropped before the index is built again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

_INDEXES = {
    "ix_usage_model_created": ["model_id", "created_at"],
    "ix_usage_provider_created": ["provider_id", "created_at"],
}


def _is_invalid(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": name},
        )
        .first()
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        for name, columns in _INDEXES.items():
            op.create_index(name, "usage_records", columns)
        return
    with op.get_context().autocommit_block():
        for name, columns in _INDEXES.items():
            if _is_invalid(name):
                op.drop_index(name, table_name="usage_records", postgresql_concurrently=True)
            op.create_index(
                name, "usage_records", columns, postgresql_concurrently=True, if_not_exists=True
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        for name in reversed(list(_INDEXES)):
            op.drop_index(name, table_name="usage_records")
        return
    with op.get_context().autocommit_block():
        for name in reversed(list(_INDEXES)):
            op.drop_index(
                name, table_name="usage_records", postgresql_concurrently=True, if_exists=True
            )
