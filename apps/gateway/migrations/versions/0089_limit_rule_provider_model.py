"""A quota rule may be limited to one provider or one model.

Revision ID: 0089
Revises: 0088
Create Date: 2026-11-21

``limit_rules.provider_id`` and ``limit_rules.model_id`` are an optional filter on the
existing rules: a rule that names them counts, and limits, only the requests served by that
provider (or that model). Both null (every existing row) keeps the rule's meaning exactly.
A rule that names a model stores the model alone (a check constraint says so): the model
decides, so repointing a model to another provider keeps its rule.

Both foreign keys are RESTRICT: cascading would silently remove a cost cap with the model, and
nulling would widen it to all usage. The admin API answers a delete of a model or provider that
a rule names with a 409 listing the rules.

``usage_records.provider_id`` is new: the provider that served the row, written at record
time, so a provider's rule sums history that survives a model being repointed or deleted. It is
backfilled here from each row's model's current provider, which is best effort for history:
a row whose model was already deleted stays null, and a model repointed earlier is attributed to
its current provider.

The uniqueness index gains the two filter columns (a provider's rule, a model's rule and the
unfiltered rule are distinct). It is rebuilt on both dialects, as in 0068: SQLite's batch
rebuild cannot reflect an expression index, so it would vanish with the old table otherwise.

``ix_usage_model_created`` and ``ix_usage_provider_created`` serve the ledger reads of a
filtered rule (seeding, rebuilding, the database fallback). On PostgreSQL they are built
CONCURRENTLY, outside the migration's transaction, so a large ledger is not locked against
writes while they build.

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


_MODEL_ALONE = "ck_limit_rules_model_alone"


def _concurrently(create: bool, name: str, columns: list[str]) -> None:
    """Build or drop an index on usage_records; CONCURRENTLY on PostgreSQL."""
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            if create:
                op.create_index(
                    name, "usage_records", columns, postgresql_concurrently=True, if_not_exists=True
                )
            else:
                op.drop_index(
                    name,
                    table_name="usage_records",
                    postgresql_concurrently=True,
                    if_exists=True,
                )
    elif create:
        op.create_index(name, "usage_records", columns)
    else:
        op.drop_index(name, table_name="usage_records")


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="limit_rules")
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("model_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_limit_rules_provider_id", "providers", ["provider_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.create_foreign_key(
            "fk_limit_rules_model_id", "models", ["model_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.create_check_constraint(_MODEL_ALONE, "model_id IS NULL OR provider_id IS NULL")
    _create_identity_index(with_filter=True)

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_usage_records_provider_id",
            "providers",
            ["provider_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute(
        "UPDATE usage_records SET provider_id = "
        "(SELECT models.provider_id FROM models WHERE models.id = usage_records.model_id) "
        "WHERE model_id IS NOT NULL"
    )
    _concurrently(True, "ix_usage_model_created", ["model_id", "created_at"])
    _concurrently(True, "ix_usage_provider_created", ["provider_id", "created_at"])


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
    _concurrently(False, "ix_usage_provider_created", ["provider_id", "created_at"])
    _concurrently(False, "ix_usage_model_created", ["model_id", "created_at"])
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_constraint("fk_usage_records_provider_id", type_="foreignkey")
        batch_op.drop_column("provider_id")
    op.drop_index(_INDEX, table_name="limit_rules")
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.drop_constraint(_MODEL_ALONE, type_="check")
        batch_op.drop_constraint("fk_limit_rules_model_id", type_="foreignkey")
        batch_op.drop_constraint("fk_limit_rules_provider_id", type_="foreignkey")
        batch_op.drop_column("model_id")
        batch_op.drop_column("provider_id")
    _create_identity_index(with_filter=False)
