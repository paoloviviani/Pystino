"""redaction rules per provider, model, group, user and key

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-27

See docs/adr/0038-scoped-redaction.md.

Three things, all additive:

``redaction_rules`` carries a whole ``RedactionPolicy`` as JSON per scope. Shaped
like ``limit_rules`` because an operator already understands that table, minus
the time axis quotas need and minus a ``global`` scope — ``redaction_config`` is
the global scope, and a second home for one value is how a screen ends up
disagreeing with itself about which is in force.

``usage_records`` gains ``redaction_scope`` and ``redaction_rule_id``. The rules
table is mutable, so the trail that answers "why was this request redacted" has
to live on the request rather than on the rule.

No backfill and no data migration: the table starts empty, which means "no scoped
rules", which behaves exactly as the deployment policy alone did before.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "redaction_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        # VARCHAR + CHECK rather than a native PostgreSQL enum, matching every
        # other enum here: adding a scope later is then a code change, not a
        # migration that takes a lock.
        sa.Column(
            "scope",
            sa.Enum(
                "provider",
                "model",
                "group",
                "user",
                "api_key",
                name="redaction_scope",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        # Not a foreign key: it points at one of five tables, as
        # limit_rules.scope_id does. The scope says which.
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        # SET NULL, as everywhere else that records who decided something: erasing
        # a user must not erase the record of what they changed.
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # One rule per subject, so two rules that disagree are a 409 rather than a
    # coin toss at request time.
    op.create_index(
        "uq_redaction_rules_identity", "redaction_rules", ["scope", "scope_id"], unique=True
    )
    op.create_index(
        "ix_redaction_rules_lookup", "redaction_rules", ["is_active", "scope", "scope_id"]
    )

    op.add_column(
        "usage_records", sa.Column("redaction_scope", sa.String(length=32), nullable=True)
    )
    op.add_column("usage_records", sa.Column("redaction_rule_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_records", "redaction_rule_id")
    op.drop_column("usage_records", "redaction_scope")
    op.drop_index("ix_redaction_rules_lookup", table_name="redaction_rules")
    op.drop_index("uq_redaction_rules_identity", table_name="redaction_rules")
    op.drop_table("redaction_rules")
