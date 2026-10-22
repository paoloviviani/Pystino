"""A global rule may apply to each user (issue P7, on ADR 0098's column).

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-21

0061 gave ``limit_rules.applies_to`` the values ``aggregate`` and
``each_member`` and a check constraint that only let ``each_member`` sit on a
group rule. This adds ``each_user``, which means something only for the global
scope: one budget per person, counted over everything they spend.

No column changes and no data to move: every existing row is ``aggregate`` or
``each_member`` and stays valid under the wider constraint. The uniqueness
index already includes ``applies_to``, so a global aggregate rule and a global
per-user rule for the same metric and window coexist without touching it. Only
the check is replaced, through batch mode so SQLite rebuilds the table.

That rebuild is the trap: batch mode cannot reflect an expression index back, so
on SQLite it silently drops ``uq_limit_rules_identity`` along with the old table
(0061 hit the same thing). The index is recreated afterwards on SQLite only; on
PostgreSQL the plain ALTER leaves it alone. A test that inserts a duplicate rule
after the migration is what caught it here.

The downgrade refuses while a per-user rule exists. Narrowing the constraint
over such a row would fail on PostgreSQL, and deleting the rule instead would
silently lift a ceiling somebody set on everyone; which to do is not this
migration's call, so the operator removes the rules first.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

_NAME = "ck_limit_rules_applies_to_group"


def _recreate_identity_index_if_lost() -> None:
    """Put the unique index back where a SQLite table rebuild dropped it."""
    if op.get_bind().dialect.name == "postgresql":
        return
    op.create_index(
        "uq_limit_rules_identity",
        "limit_rules",
        [
            "scope",
            sa.text("coalesce(scope_id, '00000000-0000-0000-0000-000000000000')"),
            "metric",
            sa.text("coalesce(window_seconds, -1)"),
            sa.text("coalesce(period, '')"),
            "applies_to",
        ],
        unique=True,
    )


def upgrade() -> None:
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.drop_constraint(_NAME, type_="check")
        batch_op.create_check_constraint(
            _NAME,
            "(applies_to = 'aggregate')"
            " OR (applies_to = 'each_member' AND scope = 'group')"
            " OR (applies_to = 'each_user' AND scope = 'global')",
        )
    _recreate_identity_index_if_lost()


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT count(*) FROM limit_rules WHERE applies_to = 'each_user'")
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"{count} quota rule(s) apply to each user; delete them before downgrading, "
            "since the older schema has no way to store them"
        )
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.drop_constraint(_NAME, type_="check")
        batch_op.create_check_constraint(
            _NAME,
            "(applies_to = 'aggregate') OR (applies_to = 'each_member' AND scope = 'group')",
        )
    _recreate_identity_index_if_lost()
