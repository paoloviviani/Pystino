"""Capabilities became per-group flags (ADR 0096, amended).

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-14

Before this revision, who held a capability was the deployment setting
(``everyone`` | ``groups``, folded row-over-env) applied to the flags. After
it, the flag alone decides and the setting is the default new groups are
created with. The upgrade must not change what is in force, so for each
capability it reads the folded mode exactly as 0059 left it — the newest
``oidc_config`` row's column, else ``GATEWAY_ACCESS__*``, else ``everyone``:

- ``everyone`` (today's default): the flag is set **on** for every existing
  group, because that is what was in force. The default for new groups
  becomes ``on``.
- ``groups``: the stored flags stand untouched, and the default for new
  groups becomes ``off``.

The resulting defaults are recorded in one appended carry-forward row (the
same append the console's saves make), so the poll keeps reading them and a
newly created group defaults the way the upgrade left things. The migrate
service carries the two ``GATEWAY_ACCESS__*`` variables for exactly this
read (deploy/compose.yaml, like 0058's OIDC passthrough). No schema change:
the columns this revision writes are 0059's.
"""

from __future__ import annotations

import os
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

_oidc_config = sa.Table(
    "oidc_config",
    sa.MetaData(),
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("join_mode", sa.String(16)),
    sa.Column("required_groups", sa.JSON),
    sa.Column("group_fallback", sa.String(16)),
    sa.Column("groups_claim", sa.String(255)),
    sa.Column("admin_groups", sa.JSON),
    sa.Column("group_mappings", sa.JSON),
    sa.Column("code_agents_mode", sa.String(16)),
    sa.Column("api_keys_mode", sa.String(16)),
    sa.Column("auto_provision", sa.Boolean),
    sa.Column("unknown_user_policy", sa.String(16)),
    sa.Column("reason", sa.String(500)),
    sa.Column("created_by", sa.Uuid),
    sa.Column("created_at", sa.DateTime),
)

_groups = sa.Table(
    "groups",
    sa.MetaData(),
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("code_agents_allowed", sa.Boolean),
    sa.Column("api_keys_allowed", sa.Boolean),
)

REASON = "migration 0060: capabilities became per-group"

#: The 0.7.x vocabulary, still meaningful for one release.
_LEGACY = {"everyone": "on", "groups": "off"}


def _fold_mode(stored: object, env_var: str) -> str:
    """The mode as 0059 left it — normalised to ``on``/``off``: the newest
    row's column, else the environment, else ``everyone`` (``on``). A
    spelling the check constraint would not have let through the API reads
    as unset — the environment stands, the fold's own log-and-defer rule."""
    raw = stored if stored is not None else os.environ.get(env_var, "").strip()
    if raw in ("everyone", "groups"):
        return _LEGACY[str(raw)]
    if raw in ("on", "off"):
        return str(raw)
    return "on"


def upgrade() -> None:
    # The values this revision writes (`on`/`off`) sit outside 0059's check
    # constraint (`everyone`/`groups`), so the constraint is widened here —
    # the old spellings stay valid, the new ones become valid — before any
    # row is written, and dropped-with-the-columns in the downgrade. Without
    # this, the very first append fails the check on every dialect.
    with op.batch_alter_table("oidc_config") as batch:
        batch.drop_constraint("ck_oidc_config_api_keys_mode", type_="check")
        batch.drop_constraint("ck_oidc_config_code_agents_mode", type_="check")
        batch.create_check_constraint(
            "ck_oidc_config_code_agents_mode",
            "code_agents_mode IS NULL OR code_agents_mode IN ('on', 'off', 'everyone', 'groups')",
        )
        batch.create_check_constraint(
            "ck_oidc_config_api_keys_mode",
            "api_keys_mode IS NULL OR api_keys_mode IN ('on', 'off', 'everyone', 'groups')",
        )

    # Imported here rather than at module level (0041's way): a migration
    # keeps loading against the application tree as it stands, and utcnow()
    # is what the model's own column default writes. `sa.func.now()` would
    # spell CURRENT_TIMESTAMP on SQLite ("YYYY-MM-DD HH:MM:SS"), which does
    # not sort against the ISO stamps the ORM writes — newest-first could
    # then pick the seeded row and these defaults would never take.
    from gateway.types import utcnow

    bind = op.get_bind()
    stored_row = (
        bind.execute(sa.select(_oidc_config).order_by(_oidc_config.c.created_at.desc()).limit(1))
        .mappings()
        .first()
    )
    code_mode = _fold_mode(
        stored_row["code_agents_mode"] if stored_row is not None else None,
        "GATEWAY_ACCESS__CODE_AGENTS",
    )
    api_mode = _fold_mode(
        stored_row["api_keys_mode"] if stored_row is not None else None,
        "GATEWAY_ACCESS__API_KEYS",
    )

    if code_mode == "on":
        bind.execute(sa.update(_groups).values(code_agents_allowed=True))
    if api_mode == "on":
        bind.execute(sa.update(_groups).values(api_keys_allowed=True))

    def carry(column: str) -> object:
        return stored_row[column] if stored_row is not None else None

    # Columns the carry-forward leaves unset are OMITTED, not bound: a JSON
    # column bound with None stores the four characters `null`, which the ORM
    # would then read back as a decision. An omitted column is a SQL NULL,
    # which is what "this row does not decide" means (0058's lesson).
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "code_agents_mode": code_mode,
        "api_keys_mode": api_mode,
        "reason": REASON,
        "created_at": utcnow(),
    }
    for column in (
        "join_mode",
        "required_groups",
        "group_fallback",
        "groups_claim",
        "admin_groups",
        "group_mappings",
        "auto_provision",
        "unknown_user_policy",
        "created_by",
    ):
        value = carry(column)
        if value is not None:
            values[column] = value
    bind.execute(sa.insert(_oidc_config).values(**values))


def downgrade() -> None:
    """The constraint narrows back; the row and the flags stay.

    The appended row is configuration, not schema; deleting it by reason
    would delete what the operator wrote since. The flags it set are the
    state the previous revision's setting implied, and a downgrade that
    cleared them would take capabilities away the deployment had granted.
    A row carrying `on`/`off` would violate the narrowed check on the next
    write that touches it, so the downgrade replaces the mode columns'
    spellings back to the 0.7.x vocabulary first.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE oidc_config SET code_agents_mode = 'everyone' WHERE code_agents_mode = 'on'"
        )
    )
    bind.execute(
        sa.text("UPDATE oidc_config SET code_agents_mode = 'groups' WHERE code_agents_mode = 'off'")
    )
    bind.execute(
        sa.text("UPDATE oidc_config SET api_keys_mode = 'everyone' WHERE api_keys_mode = 'on'")
    )
    bind.execute(
        sa.text("UPDATE oidc_config SET api_keys_mode = 'groups' WHERE api_keys_mode = 'off'")
    )
    with op.batch_alter_table("oidc_config") as batch:
        batch.drop_constraint("ck_oidc_config_api_keys_mode", type_="check")
        batch.drop_constraint("ck_oidc_config_code_agents_mode", type_="check")
        batch.create_check_constraint(
            "ck_oidc_config_code_agents_mode",
            "code_agents_mode IS NULL OR code_agents_mode IN ('everyone', 'groups')",
        )
        batch.create_check_constraint(
            "ck_oidc_config_api_keys_mode",
            "api_keys_mode IS NULL OR api_keys_mode IN ('everyone', 'groups')",
        )
