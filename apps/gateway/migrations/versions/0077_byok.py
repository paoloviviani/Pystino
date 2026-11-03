"""Bring your own key: private providers and models, and their ledger flag.

Revision ID: 0077
Revises: 0076
Create Date: 2026-11-03

A user holding the ``byok`` capability adds their own providers (their own API
key) and models and uses them privately.

* ``providers`` and ``models`` gain ``owner_kind`` / ``owner_id`` (both null =
  a deployment row, today's behaviour, unchanged) and ``admin_disabled`` (an
  administrator's hold, which the owner's own ``is_active`` cannot undo).
  ``owner_id`` references ``users`` ON DELETE CASCADE. The cascade is a
  backstop only: ``models.provider_id`` is RESTRICT, and PostgreSQL does not
  promise an order between the two cascades, so the erasure path deletes a
  user's private models before their providers explicitly.

* **Uniqueness.** The unique index on ``name`` becomes two partial unique
  indexes: ``name`` among deployment rows (``owner_id IS NULL``), and
  ``(owner_kind, owner_id, name)`` among private ones. NULLs are distinct in
  both dialects, so one index over ``(owner_id, name)`` could not have kept
  deployment names unique. The plain ``ix_*_name`` index stays, non-unique,
  for lookups. Partial indexes are supported by SQLite and PostgreSQL alike.

* ``ck_*_owner`` keeps the pair consistent (both or neither, ``user`` the only
  kind), and a private model is never an alias. That a private model's provider
  has the same owner is a cross-table fact no CHECK can state; the write path
  enforces it and the access predicate re-checks it on every read.

* ``usage_records.byok`` marks a request served by the caller's own key.
  Quotas never count such rows and no group or administrator report adds them
  up; a column so no report needs a join to find out.

* ``groups.byok_allowed`` (existing groups OFF: nothing is widened) and the
  deployment-wide defaults on ``oidc_config`` (``byok_mode``,
  ``byok_private_networks``; null defers to the environment).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEPLOYMENT = sa.text("owner_id IS NULL")
_PRIVATE = sa.text("owner_id IS NOT NULL")


def _owner_columns(batch_op: Any, table: str) -> None:
    batch_op.add_column(sa.Column("owner_kind", sa.String(length=16), nullable=True))
    batch_op.add_column(sa.Column("owner_id", sa.Uuid(), nullable=True))
    batch_op.add_column(
        sa.Column(
            "admin_disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        )
    )
    batch_op.create_foreign_key(
        f"fk_{table}_owner_id_users", "users", ["owner_id"], ["id"], ondelete="CASCADE"
    )


def upgrade() -> None:
    # The old unique indexes first: SQLite rebuilds the table inside the batch
    # blocks below and carries every index across.
    op.drop_index("ix_providers_name", table_name="providers")
    op.drop_index("ix_models_name", table_name="models")

    with op.batch_alter_table("providers", schema=None) as batch_op:
        _owner_columns(batch_op, "providers")
        batch_op.create_check_constraint(
            "ck_providers_owner",
            "(owner_kind IS NULL AND owner_id IS NULL) OR "
            "(owner_kind = 'user' AND owner_id IS NOT NULL)",
        )
    with op.batch_alter_table("models", schema=None) as batch_op:
        _owner_columns(batch_op, "models")
        batch_op.create_check_constraint(
            "ck_models_owner",
            "(owner_kind IS NULL AND owner_id IS NULL) OR "
            "(owner_kind = 'user' AND owner_id IS NOT NULL AND NOT is_alias)",
        )

    for table in ("providers", "models"):
        op.create_index(f"ix_{table}_name", table, ["name"], unique=False)
        op.create_index(
            f"uq_{table}_name_deployment",
            table,
            ["name"],
            unique=True,
            sqlite_where=_DEPLOYMENT,
            postgresql_where=_DEPLOYMENT,
        )
        op.create_index(
            f"uq_{table}_owner_name",
            table,
            ["owner_kind", "owner_id", "name"],
            unique=True,
            sqlite_where=_PRIVATE,
            postgresql_where=_PRIVATE,
        )
        op.create_index(f"ix_{table}_owner", table, ["owner_kind", "owner_id"], unique=False)

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("byok", sa.Boolean(), nullable=False, server_default=sa.text("false"))
        )

    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "byok_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")
            )
        )

    with op.batch_alter_table("oidc_config", schema=None) as batch_op:
        batch_op.add_column(sa.Column("byok_mode", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("byok_private_networks", sa.Boolean(), nullable=True))
        batch_op.create_check_constraint(
            "ck_oidc_config_byok_mode",
            "byok_mode IS NULL OR byok_mode IN ('on', 'off', 'everyone', 'groups')",
        )


def downgrade() -> None:
    with op.batch_alter_table("oidc_config", schema=None) as batch_op:
        batch_op.drop_constraint("ck_oidc_config_byok_mode", type_="check")
        batch_op.drop_column("byok_private_networks")
        batch_op.drop_column("byok_mode")
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_column("byok_allowed")
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("byok")

    private = op.get_bind().execute(
        sa.text("SELECT count(*) FROM models WHERE owner_id IS NOT NULL")
    ).scalar_one() + op.get_bind().execute(
        sa.text("SELECT count(*) FROM providers WHERE owner_id IS NOT NULL")
    ).scalar_one()
    if private:
        raise RuntimeError(
            "0077 downgrade refused: private (BYOK) providers or models exist, and restoring "
            "a globally unique name could collide them with deployment rows. Delete them "
            "first (DELETE FROM models WHERE owner_id IS NOT NULL, then providers) and re-run."
        )
    for table in ("providers", "models"):
        op.drop_index(f"ix_{table}_owner", table_name=table)
        op.drop_index(f"uq_{table}_owner_name", table_name=table)
        op.drop_index(f"uq_{table}_name_deployment", table_name=table)
        op.drop_index(f"ix_{table}_name", table_name=table)
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_constraint("ck_models_owner", type_="check")
        batch_op.drop_constraint("fk_models_owner_id_users", type_="foreignkey")
        batch_op.drop_column("admin_disabled")
        batch_op.drop_column("owner_id")
        batch_op.drop_column("owner_kind")
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_constraint("ck_providers_owner", type_="check")
        batch_op.drop_constraint("fk_providers_owner_id_users", type_="foreignkey")
        batch_op.drop_column("admin_disabled")
        batch_op.drop_column("owner_id")
        batch_op.drop_column("owner_kind")
    op.create_index("ix_providers_name", "providers", ["name"], unique=True)
    op.create_index("ix_models_name", "models", ["name"], unique=True)
