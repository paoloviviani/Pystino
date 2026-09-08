"""the deployment policy becomes a rule scoped to everything

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-27

See ADR 0039.

Two changes, and they are the same change seen from either end.

``redaction_rules.scope_id`` becomes nullable, for exactly one scope: ``all``,
whose subject is every request. A CHECK ties the two together, and the identity
index gains the COALESCE that ``limit_rules`` already uses — SQL treats NULLs as
distinct, so a plain UNIQUE would accept two catch-all rules and leave the
gateway picking between them at request time.

``redaction_config.policy`` is dropped. It held the deployment-wide policy, which
is now the ``all`` rule: one concept, one shape, one screen. The column's data is
not migrated into a rule, and that is deliberate rather than lazy — the old
default protected everything the engine found, the new one protects nothing until
somebody says otherwise, and silently converting the first into a rule would
carry a policy nobody chose into a system whose whole point is that policy is
chosen. A deployment that wants the old behaviour writes one rule and can see it.

``redaction_config`` keeps its engine column and its history. Which engine is
running was never a per-scope question.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("redaction_rules") as batch:
        batch.alter_column("scope_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(
            "ck_redaction_rules_scope_id_presence",
            "(scope = 'all' AND scope_id IS NULL) OR (scope <> 'all' AND scope_id IS NOT NULL)",
        )

    op.drop_index("uq_redaction_rules_identity", table_name="redaction_rules")
    op.create_index(
        "uq_redaction_rules_identity",
        "redaction_rules",
        ["scope", sa.text("coalesce(scope_id, '00000000-0000-0000-0000-000000000000')")],
        unique=True,
    )

    op.drop_column("redaction_config", "policy")


def downgrade() -> None:
    op.add_column("redaction_config", sa.Column("policy", sa.JSON(), nullable=True))

    op.drop_index("uq_redaction_rules_identity", table_name="redaction_rules")
    op.create_index(
        "uq_redaction_rules_identity", "redaction_rules", ["scope", "scope_id"], unique=True
    )

    # Catch-all rules cannot survive a downgrade: the column they need is going
    # away. Removed rather than left to violate the constraint being restored.
    op.execute("DELETE FROM redaction_rules WHERE scope = 'all'")
    with op.batch_alter_table("redaction_rules") as batch:
        batch.drop_constraint("ck_redaction_rules_scope_id_presence", type_="check")
        batch.alter_column("scope_id", existing_type=sa.Uuid(), nullable=False)
