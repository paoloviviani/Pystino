"""Which models this deployment extracts and embeds with, as an admin decision.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-09

One append-only table (ADR 0062), the third of its shape here after
``redaction_config`` (ADR 0033) and ``oidc_config`` (ADR 0048). The pattern is
not repeated for tidiness — it is repeated because each of these tables records
a deliberate administrative act whose *history* is the interesting part, and a
mutable row can only ever answer for the most recent one.

Why it matters more here than for either of the others: changing the embedding
model breaks nothing at the moment of the change and makes every base indexed
before it incomparable with every base indexed after. The question "which model
was this built with, and who moved the default, and when" gets asked while
staring at results that have quietly stopped making sense.

**This migration seeds nothing, deliberately.** Writing today's environment
values into the table would silently pin them, so a later change to
``GATEWAY_KNOWLEDGE__*`` would stop taking effect for a reason nobody could
see. No row means the environment decides — which is also what makes applying
this to a deployment that never turns the feature on a no-op.

Every policy column is nullable, and null means **"this row does not decide"**
rather than "off". That is copied from ``oidc_config``: it lets an
administrator turn one knob without restating the other four, and stops a
narrow change from reverting a wide one.

What is deliberately not here: the extractor's *endpoint*. It stays in the
environment beside the redaction endpoint, for the reason recorded there — an
endpoint that can be typed into a console is an endpoint that can be pointed at
a logger. Which model is a console decision; which host receives the bytes is
not.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        # RESTRICT, matching knowledge_bases.embedding_model_id: a default
        # pointing at a deleted model would make every new base unindexable,
        # and the failure would surface at first upload rather than here.
        sa.Column(
            "embedding_model_id",
            sa.Uuid(),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # SET NULL: null already means "the built-in extractor", which is the
        # safe default — it is the only backend that never sends a document
        # anywhere (ADR 0055).
        sa.Column(
            "extractor_model_id",
            sa.Uuid(),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # A registry name, not an enum: a second backend arrives as an
        # installed implementation and cannot be enumerated in a schema written
        # before it exists. Same reasoning as redaction_config.engine.
        sa.Column("vector_store", sa.String(64), nullable=True),
        sa.Column("chunk_chars", sa.Integer(), nullable=True),
        sa.Column("chunk_overlap", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        # SET NULL rather than CASCADE: erasing a user under GDPR must not
        # delete the record of what they changed.
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_config_created", "knowledge_config", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_config_created", table_name="knowledge_config")
    op.drop_table("knowledge_config")
