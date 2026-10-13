"""Self-mappings for the groups claims were feeding (the mapping-only rule).

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-13

The rule from here on: a group name from a token reaches a local group only
through ``oidc_config.group_mappings``. Every deployment upgraded by this
revision has groups a claim used to feed by *name* — the claim resolved
because a local group happened to carry the name — and this migration
restates exactly that resolution as what the rule needs: one self-mapping
(name -> name) per local group that has ``source = oidc``, or any membership
with ``source = oidc``. A group nothing claimed stays unmapped; one whose
name already self-maps is skipped. The additions ride one appended
``oidc_config`` row (the newest row carried forward, so the join answers
survive), the same append the console's saves and the map endpoint make.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

# The columns this migration reads and writes, reflected rather than imported:
# a migration must keep loading against the schema as it stands at 0057, not
# against the application model it may have moved on from.
_oidc_config = sa.Table(
    "oidc_config",
    sa.MetaData(),
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("auto_provision", sa.Boolean),
    sa.Column("unknown_user_policy", sa.String(16)),
    sa.Column("join_mode", sa.String(16)),
    sa.Column("required_groups", sa.JSON),
    sa.Column("group_fallback", sa.String(16)),
    sa.Column("groups_claim", sa.String(255)),
    sa.Column("admin_groups", sa.JSON),
    sa.Column("group_mappings", sa.JSON),
    sa.Column("reason", sa.String(500)),
    sa.Column("created_by", sa.Uuid),
    sa.Column("created_at", sa.DateTime),
)

_groups = sa.Table(
    "groups",
    sa.MetaData(),
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("name", sa.String(255)),
    sa.Column("source", sa.String(16)),
)

_memberships = sa.Table(
    "memberships",
    sa.MetaData(),
    sa.Column("group_id", sa.Uuid),
    sa.Column("source", sa.String(16)),
)

REASON = "migration 0058: self-mappings for groups claimed by name"


def upgrade() -> None:
    bind = op.get_bind()
    eligible = {
        row.name
        for row in bind.execute(
            sa.select(_groups.c.name).where(
                sa.or_(
                    _groups.c.source == "oidc",
                    sa.select(_memberships.c.group_id)
                    .where(
                        _memberships.c.group_id == _groups.c.id,
                        _memberships.c.source == "oidc",
                    )
                    .exists(),
                )
            )
        )
    }
    if not eligible:
        return
    stored = bind.execute(
        sa.select(_oidc_config).order_by(_oidc_config.c.created_at.desc()).limit(1)
    ).mappings().first()
    current: dict[str, str] = {}
    if stored is not None and stored["group_mappings"] is not None:
        for pair in stored["group_mappings"]:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                idp, local = pair
                if isinstance(idp, str) and isinstance(local, str) and idp and local:
                    current[idp] = local
    additions = sorted(name for name in eligible if current.get(name) != name)
    if not additions:
        return

    def carry(column: str) -> object:
        return stored[column] if stored is not None else None

    pairs = [[idp, local] for idp, local in current.items()] + [
        [name, name] for name in additions
    ]
    bind.execute(
        sa.insert(_oidc_config).values(
            id=uuid.uuid4(),
            auto_provision=carry("auto_provision"),
            unknown_user_policy=carry("unknown_user_policy"),
            join_mode=carry("join_mode"),
            required_groups=carry("required_groups"),
            group_fallback=carry("group_fallback"),
            groups_claim=carry("groups_claim"),
            admin_groups=carry("admin_groups"),
            group_mappings=pairs,
            reason=REASON,
            created_by=carry("created_by"),
            created_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    """Nothing to undo, and nothing that could be undone honestly.

    The appended row is configuration, not schema: unmapping is the console's
    decision (PUT the mappings without the pair), and a downgrade that deleted
    rows by reason would delete what the operator wrote since. The column
    shapes this revision touches are unchanged.
    """
    pass
