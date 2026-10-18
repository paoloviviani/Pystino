"""Model aliases: an `is_alias` row, its target, and what the caller asked for.

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-18

ADR 0101. An alias is a row in ``models`` that names something to serve without
being able to serve it itself: it resolves to another row in the same table
through the new ``model_route_targets`` child table, and its access is granted
independently of that target's.

Three parts, and the middle one is why this migration is not smaller:

* ``models.is_alias``, with ``provider_id`` and ``upstream_model`` nullable, and
  a check constraint holding the two shapes apart. This is the cost of one
  table for both, and the constraint is what stops a hand-edited row being
  half an alias.
* ``model_route_targets`` — a list for one step, because routing later adds
  rows rather than migrating a column away (ADR 0101, "Designed for routing").
  Exactly one row per alias is enforced by the save path, not here: a unique
  index on ``(alias_id, position)`` would accept a second target at position 1.
* ``usage_records.requested_model`` — the alias name the caller sent, beside the
  target that ``model_name`` now records. A string, not a foreign key, so the
  ledger still reads correctly after an alias is retargeted or deleted.

**On the indexes over ``provider_id`` and ``upstream_model``** (asked for by the
implementation notes): there are none that constrain a value. ``models`` carries
exactly ``ix_models_name`` (unique), ``ix_models_provider_id`` (plain b-tree,
from 0003) and the primary key. So there is no unique-per-provider or
unique-per-upstream index that a NULL could collide in, and in PostgreSQL a plain
index simply omits NULL rows rather than indexing them — which is what a
"delete a provider, keep its models" sweep would want to find. The one thing a
future unique index over either column would have to keep in mind is that
PostgreSQL treats NULLs as *distinct*, so two aliases could both pass such a
constraint; nothing here relies on that either way.

The downgrade **refuses** when any alias exists. ``NOT NULL`` cannot be restored
over rows that have no provider, and deleting an operator's aliases to make a
rollback succeed is not a decision a migration should take on its own.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The literal of the column's name in `gateway.models.ModelDef.is_alias`,
#: inlined for the same reason 0062 inlines its issuer constant: a migration
#: must keep meaning what it meant when written.
_IS_ALIAS = "is_alias"


def upgrade() -> None:
    # One batch block, so SQLite rebuilds `models` once rather than three times.
    # The rebuild is what makes the check constraint possible there at all:
    # SQLite cannot ADD CONSTRAINT to a table that exists.
    #
    # The server default fills `is_alias` on every existing row, so this is a
    # no-op for the data on either dialect — which matters, because "every
    # existing model is a served model" is exactly the invariant the constraint
    # below then asserts rather than assumes.
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_alias",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )
        # A served model keeps both; an alias has neither. Nothing backfills
        # these, so an existing row is unaffected by dropping NOT NULL.
        batch_op.alter_column("provider_id", existing_type=sa.Uuid(), nullable=True)
        batch_op.alter_column(
            "upstream_model", existing_type=sa.String(length=255), nullable=True
        )
        batch_op.create_check_constraint(
            "ck_models_alias_or_served",
            "(is_alias AND provider_id IS NULL AND upstream_model IS NULL) OR "
            "(NOT is_alias AND provider_id IS NOT NULL AND upstream_model IS NOT NULL)",
        )

    # After the rebuild, so the foreign keys below have a `models` to point at
    # while SQLite is doing its table swap.
    op.create_table(
        "model_route_targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("alias_id", sa.Uuid(), nullable=False),
        sa.Column("target_model_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Both cascade, and the target's is the load-bearing one: deleting a
        # target removes the route row and leaves the alias standing with its
        # grants, which is how "target deleted" becomes an *unavailable* alias
        # (ADR 0101, decision 5) rather than a deleted name.
        sa.ForeignKeyConstraint(["alias_id"], ["models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_model_id"], ["models.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias_id", "position", name="uq_model_route_targets_alias_position"),
    )
    # "Which aliases point here" — the model detail page's `aliases_pointing_here`
    # and the delete confirmation that must name them. The unique constraint
    # above leads with `alias_id` and cannot answer this direction.
    op.create_index(
        "ix_model_route_targets_target",
        "model_route_targets",
        ["target_model_id"],
        unique=False,
    )

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        # Nullable with no default: a row written before this migration named a
        # model directly, and there is no alias for it to record. Backfilling
        # `model_name` here would put an alias name on an ordinary request.
        batch_op.add_column(sa.Column("requested_model", sa.String(length=255), nullable=True))


def downgrade() -> None:
    aliases = sa.table(
        "models",
        sa.column(_IS_ALIAS, sa.Boolean()),
    )
    held = op.get_bind().execute(
        sa.select(sa.func.count()).select_from(aliases).where(aliases.c[_IS_ALIAS].is_(True))
    ).scalar_one()
    if held:
        raise RuntimeError(
            "0063 downgrade refused: the models table holds alias rows, which have no "
            "provider or upstream model. Restoring NOT NULL would need them deleted or "
            "repointed first, and neither is this migration's decision. Remove the aliases "
            "(DELETE FROM model_route_targets for each, then the models rows) and re-run."
        )

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("requested_model")

    op.drop_index("ix_model_route_targets_target", table_name="model_route_targets")
    op.drop_table("model_route_targets")

    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_constraint("ck_models_alias_or_served", type_="check")
        batch_op.alter_column(
            "upstream_model", existing_type=sa.String(length=255), nullable=False
        )
        batch_op.alter_column("provider_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.drop_column(_IS_ALIAS)
