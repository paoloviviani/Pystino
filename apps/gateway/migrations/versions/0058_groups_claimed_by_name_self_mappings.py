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
with ``source = oidc``. Three groups stay out, each because a self-mapping
would either change nothing or make things worse:

- **already a mapping key**, whatever its target: the appended pair would sit
  after the operator's in the stored row, and the fold's last-entry-wins
  would silently reroute the mapping;
- **already a mapping target** of another IdP name: its OIDC memberships
  came through that mapping, not by name, so the self-mapping would hand the
  bare name the vouch the mapping was written to keep off the claims;
- **required and manual under a `group` gate**: 0.5.0 already dropped
  same-name claims for required names, so such a group was never fed by its
  own name — the self-mapping would reopen the spoof the drop exists to stop.

The additions ride one appended ``oidc_config`` row (the newest row carried
forward, so the join answers survive), the same append the console's saves
and the map endpoint make.

The group answers read from the newest row defer to the environment per
field, exactly as ``effective_policy`` folds them: a null column stands
down, and ``GATEWAY_OIDC__JOIN_MODE`` / ``GATEWAY_OIDC__REQUIRED_GROUPS``
answer instead — which is how a deployment running ``group`` mode from the
environment alone (no console decision, and the ``migrate`` service carries
those variables since this revision) still gets its required manual groups
kept out of the self-mappings.
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


def _required_from_env(raw: str | None) -> set[str]:
    """`GATEWAY_OIDC__REQUIRED_GROUPS` as the environment spells it: a JSON
    list or a comma-separated list, parsed the way ``config.py`` parses it
    (its ``field_validator`` mode="before"), blanks dropped."""
    if raw is None or not raw.strip():
        return set()
    text = raw.strip()
    if text.startswith("["):
        import json

        try:
            data = json.loads(text)
        except ValueError:
            return set()
        if not isinstance(data, list):
            return set()
        return {str(name).strip() for name in data if str(name).strip()}
    return {part.strip() for part in text.split(",") if part.strip()}


def upgrade() -> None:
    # Imported here rather than at module level (0041's way): a migration
    # keeps loading against the application tree as it stands, and utcnow()
    # is what the model's own column default writes. `sa.func.now()` would
    # spell CURRENT_TIMESTAMP on SQLite ("YYYY-MM-DD HH:MM:SS"), which does
    # not sort against the ISO stamps the ORM writes — newest-first could
    # then pick the seeded row and this row's mappings would never take.
    from gateway.types import utcnow

    bind = op.get_bind()
    stored = (
        bind.execute(sa.select(_oidc_config).order_by(_oidc_config.c.created_at.desc()).limit(1))
        .mappings()
        .first()
    )
    current: dict[str, str] = {}
    if stored is not None and stored["group_mappings"] is not None:
        for pair in stored["group_mappings"]:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                idp, local = pair
                if isinstance(idp, str) and isinstance(local, str) and idp and local:
                    current[idp] = local
    targets = set(current.values())
    # The fold order matches effective_policy: the row decides per field, and
    # a column it leaves null defers to the environment. The migrate service
    # carries GATEWAY_OIDC__* since this revision (deploy/compose.yaml), so a
    # deployment running group mode from the environment alone gets the same
    # answer here the gateway will fold at its next start.
    import os

    join_mode = (
        stored["join_mode"]
        if stored is not None and stored["join_mode"]
        else os.environ.get("GATEWAY_OIDC__JOIN_MODE") or None
    )
    required = (
        set(stored["required_groups"])
        if stored is not None and stored["required_groups"] is not None
        else _required_from_env(os.environ.get("GATEWAY_OIDC__REQUIRED_GROUPS"))
    )
    group_mode = join_mode == "group"

    fed = bind.execute(
        sa.select(_groups.c.name, _groups.c.source).where(
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
    ).all()

    additions: list[str] = []
    for name, source in sorted(fed):
        if name in current:
            # A mapping key already, whatever it points at: appending a
            # self-mapping would sit after the operator's pair in the stored
            # row, and the fold's last-entry-wins would silently reroute it.
            continue
        if name in targets:
            # Its OIDC memberships came through that mapping, not by name;
            # self-mapping it would hand the bare name the vouch the mapping
            # was written to keep off the claims.
            continue
        if group_mode and name in required and source == "manual":
            # 0.5.0 already dropped same-name claims for required names, so a
            # required manual group was never fed by its own name; the
            # self-mapping would reopen the spoof the drop exists to stop.
            continue
        additions.append(name)
    if not additions:
        return

    def carry(column: str) -> object:
        return stored[column] if stored is not None else None

    pairs = [[idp, local] for idp, local in current.items()] + [[name, name] for name in additions]
    # Columns the carry-forward leaves unset are OMITTED, not bound: a JSON
    # column bound with None stores the four characters `null`, which the ORM
    # would then read back as a decision ("this row says required_groups is
    # null") and the fold would crash iterating it. An omitted column is a
    # SQL NULL, which is what "this row does not decide" means.
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "group_mappings": pairs,
        "reason": REASON,
        "created_at": utcnow(),
    }
    for column in (
        "auto_provision",
        "unknown_user_policy",
        "join_mode",
        "required_groups",
        "group_fallback",
        "groups_claim",
        "admin_groups",
        "created_by",
    ):
        value = carry(column)
        if value is not None:
            values[column] = value
    bind.execute(sa.insert(_oidc_config).values(**values))


def downgrade() -> None:
    """Nothing to undo, and nothing that could be undone honestly.

    The appended row is configuration, not schema: unmapping is the console's
    decision (PUT the mappings without the pair), and a downgrade that deleted
    rows by reason would delete what the operator wrote since. The column
    shapes this revision touches are unchanged.
    """
    pass
