"""A quota rule may be limited to one provider or one model.

Revision ID: 0089
Revises: 0088
Create Date: 2026-11-21

``limit_rules.provider_id`` and ``limit_rules.model_id`` are an optional filter on the
existing rules: a rule that names them counts, and limits, only the requests served by that
provider (or that model). Both null (every existing row) keeps the rule's meaning exactly.

Both foreign keys cascade. Nulling them instead would turn "OpenAI at most EUR 500 a month"
into "everything at most EUR 500 a month" the moment the provider's last model was removed,
which is the wrong failure; a deleted model's rules have nothing left to limit.

The uniqueness index gains the two columns (a provider's rule and a model's rule are different
rules from each other and from the unfiltered one). It is rebuilt on both dialects, as in
0068: SQLite's batch rebuild cannot reflect an expression index, so it would vanish with the
old table otherwise.

``ix_usage_model_created`` serves the ledger reads of a filtered rule (seeding, rebuilding,
the database fallback), which narrow by the served model.

The downgrade refuses while a filtered rule exists: dropping the columns would turn each into
an unfiltered, much wider budget.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None

_INDEX = "uq_limit_rules_identity"
_NIL = "'00000000-0000-0000-0000-000000000000'"


def _create_identity_index(*, with_filter: bool) -> None:
    columns: list[object] = [
        "scope",
        sa.text(f"coalesce(scope_id, {_NIL})"),
        "metric",
        sa.text("coalesce(window_seconds, -1)"),
        sa.text("coalesce(period, '')"),
        "applies_to",
        "overrides_each_user",
    ]
    if with_filter:
        columns.append(sa.text(f"coalesce(provider_id, {_NIL})"))
        columns.append(sa.text(f"coalesce(model_id, {_NIL})"))
    op.create_index(_INDEX, "limit_rules", columns, unique=True)


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="limit_rules")
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("model_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_limit_rules_provider_id", "providers", ["provider_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.create_foreign_key(
            "fk_limit_rules_model_id", "models", ["model_id"], ["id"], ondelete="CASCADE"
        )
    _create_identity_index(with_filter=True)
    op.create_index("ix_usage_model_created", "usage_records", ["model_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text(
            "SELECT count(*) FROM limit_rules WHERE provider_id IS NOT NULL OR model_id IS NOT NULL"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"{count} quota rule(s) are limited to a provider or a model; delete them before "
            "downgrading, since the older schema would turn each into a rule on all usage"
        )
    op.drop_index("ix_usage_model_created", table_name="usage_records")
    op.drop_index(_INDEX, table_name="limit_rules")
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.drop_constraint("fk_limit_rules_model_id", type_="foreignkey")
        batch_op.drop_constraint("fk_limit_rules_provider_id", type_="foreignkey")
        batch_op.drop_column("model_id")
        batch_op.drop_column("provider_id")
    _create_identity_index(with_filter=False)
