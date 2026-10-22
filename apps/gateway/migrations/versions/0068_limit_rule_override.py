"""A user rule may override the global each-user ceiling for that person.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-21

``limit_rules.overrides_each_user`` marks a ``scope=user`` rule as the person's
own ceiling *in place of* every global ``each_user`` rule with the same metric
and window. A boolean rather than a fourth ``applies_to`` value: that column is a
PostgreSQL enum, a new member of which cannot be used in the transaction that
adds it, and "override" is not another whose-budget answer: the rule still caps
the person's own total, like any user rule.

The flag only means something on the user scope, so a check constraint says so
for a writer that bypasses the API. It is also part of the uniqueness index: a
person may hold an override and an ordinary (tighter) user rule for the same
metric and window, and the two are different rules. The index is therefore
rebuilt on both dialects, not only where SQLite's table rebuild loses it (the
0061/0067 trap: batch mode cannot reflect an expression index, so on SQLite the
index vanishes with the old table whether or not we meant it to).

Existing rows take ``false`` and keep their meaning. The downgrade refuses while
an override exists: dropping the flag would silently turn a raised ceiling into
the global one, or two rules into one, and which is intended is not this
migration's call.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

_CHECK = "ck_limit_rules_override_user_scope"
_INDEX = "uq_limit_rules_identity"


def _create_identity_index(*, with_override: bool) -> None:
    columns: list[object] = [
        "scope",
        sa.text("coalesce(scope_id, '00000000-0000-0000-0000-000000000000')"),
        "metric",
        sa.text("coalesce(window_seconds, -1)"),
        sa.text("coalesce(period, '')"),
        "applies_to",
    ]
    if with_override:
        columns.append("overrides_each_user")
    op.create_index(_INDEX, "limit_rules", columns, unique=True)


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="limit_rules")
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "overrides_each_user",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_check_constraint(_CHECK, "NOT overrides_each_user OR scope = 'user'")
    _create_identity_index(with_override=True)


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT count(*) FROM limit_rules WHERE overrides_each_user")
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"{count} quota rule(s) override the each-user ceiling; delete them before "
            "downgrading, since the older schema has no way to store them"
        )
    op.drop_index(_INDEX, table_name="limit_rules")
    with op.batch_alter_table("limit_rules", schema=None) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_column("overrides_each_user")
    _create_identity_index(with_override=False)
