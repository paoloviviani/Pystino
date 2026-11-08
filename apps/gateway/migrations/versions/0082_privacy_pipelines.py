"""Privacy pipelines and redaction policies (ADR 0113, section 0).

Revision ID: 0082
Revises: 0081
Create Date: 2026-11-08

Two objects, one job each. A **policy** is what to hide (no scope); a **pipeline**
is when and how (a kind, a scope, ordered rules, a fixed last rule). Every pipeline
in scope runs and the results combine, so migrating means writing the same
behaviour in the new shape, and ``test_migration_0082`` proves it byte for byte.

* ``redaction_rules`` (ADR 0038) -> ``redaction_policies``, **same id**, plus one
  administrator pipeline per kind (read, embed, chat) with the old scope and the
  last rule "everything else -> redact with this policy". A rule on ``all`` becomes
  pipelines scoped to all; provider and model scopes become a target; group, user
  and key scopes become a who. Combined in the same order the old fold used (the
  broadest scope first), so the strictest mode, the unioned patterns and the
  allow-list from the first policy come out the same.
* ``user_redaction_rules`` (ADR 0112) -> one ``user_policies`` row per user (their
  entries, in the order they were written) plus three user pipelines, one per
  kind, with the same shape.
* ``privacy_rules`` (#52, unreleased) -> rules inside per-kind administrator
  pipelines. A rule with no who or target goes in "Routing rules (kind)", scoped
  to all, in order. A rule that named who or a target gets its own pipeline with
  that scope. ``local`` actions become ``refuse`` (they failed closed without a
  local model; there is no "local" any more). A condition that said "this or
  that" (a file type and a scan, health data or IDs) is split into adjacent rules,
  which keeps first-match meaning.
* ``providers.is_local`` is dropped: Pystino has no notion of a local model.

Plain DDL plus Python-side row conversion, so it runs on SQLite as on PostgreSQL.
"""

from __future__ import annotations

import itertools
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

KINDS = ("read", "embed", "chat")
KIND_WORDS = {"read": "reading", "embed": "embedding", "chat": "chat"}
BASE = {"read": "default", "embed": "pass", "chat": "pass"}
#: The old fold's order, broadest first (ADR 0038).
SCOPE_RANK = {"all": 0, "provider": 1, "model": 2, "group": 3, "user": 4, "api_key": 5}


def _now() -> datetime:
    return datetime.now(UTC)


def upgrade() -> None:
    with op.batch_alter_table("detector_profiles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("user_available", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    op.create_table(
        "redaction_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["profile_id"], ["detector_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "user_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["detector_profiles.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("user_id", "name", name="uq_user_policies_name"),
    )
    op.create_index("ix_user_policies_user_id", "user_policies", ["user_id"])
    op.create_table(
        "privacy_pipelines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("who", sa.JSON(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("otherwise", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_privacy_pipelines_owner_id", "privacy_pipelines", ["owner_id"])
    op.create_index("ix_privacy_pipelines_kind", "privacy_pipelines", ["kind", "position"])
    op.create_table(
        "privacy_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_entity_types", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    _convert(op.get_bind())

    op.drop_table("redaction_rules")
    op.drop_table("user_redaction_rules")
    op.drop_index("ix_privacy_rules_kind_position", table_name="privacy_rules")
    op.drop_table("privacy_rules")
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("is_local")


# -- the conversion -----------------------------------------------------------------


def _convert(bind: sa.Connection) -> None:
    old_rules = sa.table(
        "redaction_rules",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("scope", sa.String()),
        sa.column("scope_id", sa.Uuid()),
        sa.column("policy", sa.JSON()),
        sa.column("is_active", sa.Boolean()),
        sa.column("reason", sa.String()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    old_user_rules = sa.table(
        "user_redaction_rules",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("name", sa.String()),
        sa.column("regex", sa.String()),
        sa.column("mode", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    old_routing = sa.table(
        "privacy_rules",
        sa.column("id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("name", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("observe_only", sa.Boolean()),
        sa.column("position", sa.Integer()),
        sa.column("conditions", sa.JSON()),
        sa.column("action", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    policies = sa.table(
        "redaction_policies",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("policy", sa.JSON()),
        sa.column("profile_id", sa.Uuid()),
        sa.column("is_active", sa.Boolean()),
        sa.column("reason", sa.String()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    user_policies = sa.table(
        "user_policies",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("rules", sa.JSON()),
        sa.column("profile_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    pipelines = sa.table(
        "privacy_pipelines",
        sa.column("id", sa.Uuid()),
        sa.column("owner_id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("name", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("position", sa.Integer()),
        sa.column("who", sa.JSON()),
        sa.column("target", sa.JSON()),
        sa.column("rules", sa.JSON()),
        sa.column("otherwise", sa.JSON()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    policy_rows: list[dict[str, Any]] = []
    user_policy_rows: list[dict[str, Any]] = []
    pipeline_rows: list[dict[str, Any]] = []
    position: dict[tuple[str | None, str], int] = {}

    def add_pipeline(
        kind: str,
        name: str,
        who: dict[str, Any],
        target: dict[str, Any],
        rules: list[dict[str, Any]],
        otherwise: dict[str, Any],
        *,
        owner: uuid.UUID | None = None,
        enabled: bool = True,
        created_at: datetime | None = None,
        created_by: uuid.UUID | None = None,
    ) -> None:
        key = (str(owner) if owner else None, kind)
        index = position.get(key, 0)
        position[key] = index + 1
        now = _now()
        pipeline_rows.append(
            {
                "id": uuid.uuid4(),
                "owner_id": owner,
                "kind": kind,
                "name": name[:120],
                "enabled": enabled,
                "position": index,
                "who": who,
                "target": target,
                "rules": rules,
                "otherwise": otherwise,
                "created_by": created_by,
                "created_at": created_at or now,
                "updated_at": created_at or now,
            }
        )

    # (a) the scoped redaction rules -> policies, and pipelines with their scope.
    rows = bind.execute(
        sa.select(*old_rules.c).order_by(old_rules.c.created_at, old_rules.c.id)
    ).all()
    ordered = sorted(rows, key=lambda r: SCOPE_RANK.get(str(r.scope), 9))
    used_names: set[str] = set()
    for row in ordered:
        scope = str(row.scope)
        base = row.name or ("All requests" if scope == "all" else f"Rule for a {scope}")
        name, n = base, 2
        while name.lower() in used_names:
            name, n = f"{base} ({n})", n + 1
        used_names.add(name.lower())
        policy_rows.append(
            {
                "id": row.id,
                "name": name,
                "policy": row.policy or {},
                "profile_id": None,
                "is_active": bool(row.is_active),
                "reason": row.reason or "",
                "created_by": row.created_by,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
        subject = [str(row.scope_id)] if row.scope_id is not None else []
        who: dict[str, Any] = {"mode": "all", "ids": []}
        target: dict[str, Any] = {"mode": "all", "providers": [], "models": []}
        if scope == "provider":
            target = {"mode": "only", "providers": subject, "models": []}
        elif scope == "model":
            target = {"mode": "only", "providers": [], "models": subject}
        elif scope in ("group", "user", "api_key"):
            who = {"mode": {"group": "groups", "user": "users", "api_key": "keys"}[scope], "ids": subject}
        for kind in KINDS:
            add_pipeline(
                kind,
                f"{name}: {KIND_WORDS[kind]}",
                who,
                target,
                [],
                {"level": BASE[kind], "redact_with": str(row.id)},
                enabled=bool(row.is_active),
                created_at=row.created_at,
                created_by=row.created_by,
            )

    # (b) users' own rules -> a user policy and three user pipelines each.
    by_user: dict[uuid.UUID, list[Any]] = {}
    for row in bind.execute(
        sa.select(*old_user_rules.c).order_by(old_user_rules.c.created_at, old_user_rules.c.id)
    ).all():
        by_user.setdefault(row.user_id, []).append(row)
    for user_id, entries in by_user.items():
        policy_id = uuid.uuid4()
        user_policy_rows.append(
            {
                "id": policy_id,
                "user_id": user_id,
                "name": "My rules",
                "rules": [
                    {"kind": e.kind, "name": e.name, "regex": e.regex, "mode": e.mode}
                    for e in entries
                ],
                "profile_id": None,
                "created_at": entries[0].created_at,
                "updated_at": entries[-1].created_at,
            }
        )
        for kind in KINDS:
            add_pipeline(
                kind,
                f"My rules ({kind})",
                {"mode": "users", "ids": [str(user_id)]},
                {"mode": "all", "providers": [], "models": []},
                [],
                {"level": BASE[kind], "redact_with": str(policy_id)},
                owner=user_id,
                created_at=entries[0].created_at,
                created_by=user_id,
            )

    # (c) routing rules (#52) -> rules inside pipelines.
    _convert_routing(bind, old_routing, add_pipeline)

    if policy_rows:
        op.bulk_insert(policies, policy_rows)
    if user_policy_rows:
        op.bulk_insert(user_policies, user_policy_rows)
    if pipeline_rows:
        op.bulk_insert(pipelines, pipeline_rows)


def _action(raw: dict[str, Any]) -> dict[str, Any]:
    kind = raw.get("type")
    if kind == "reader":
        engine = str(raw.get("reader") or "markitdown")
        return {"level": "read_with", "reader": "builtin", "engine": engine}
    # `local` failed closed without a local model; with no "local" it refuses.
    return {"level": "refuse"}


def _lookup(bind: sa.Connection, table: str, column: str, value: str, extra: str = "") -> uuid.UUID | None:
    found = bind.execute(
        sa.text(f"SELECT id FROM {table} WHERE lower({column}) = lower(:v) {extra} LIMIT 1"),  # noqa: S608
        {"v": value},
    ).first()
    if found is None:
        return None
    return found[0] if isinstance(found[0], uuid.UUID) else uuid.UUID(str(found[0]))


def _scope_of(
    bind: sa.Connection, conditions: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], bool]:
    """(who, target, the remaining conditions, whether the rule can ever fire)."""
    who: dict[str, Any] = {"mode": "all", "ids": []}
    target: dict[str, Any] = {"mode": "all", "providers": [], "models": []}
    rest: list[dict[str, Any]] = []
    for c in conditions:
        key, values, negate = c.get("key"), c.get("values") or [], bool(c.get("negate"))
        if key == "who" and not negate:
            users: list[str] = []
            groups: list[str] = []
            for v in values:
                prefix, _, name = str(v).partition(":")
                if prefix == "user":
                    found = _lookup(bind, "users", "email", name)
                    if found:
                        users.append(str(found))
                elif prefix == "group":
                    found = _lookup(bind, "groups", "name", name)
                    if found:
                        groups.append(str(found))
            if users and not groups:
                who = {"mode": "users", "ids": users}
            elif groups and not users:
                who = {"mode": "groups", "ids": groups}
            elif not users and not groups:
                return who, target, rest, False
            else:
                logger.warning("a routing rule names users and groups; its who is dropped")
        elif key == "target" and all(
            str(v).startswith(("provider:", "model:")) for v in values
        ):
            providers: list[str] = []
            models: list[str] = []
            for v in values:
                prefix, _, name = str(v).partition(":")
                if prefix == "provider":
                    found = _lookup(bind, "providers", "name", name, "AND owner_id IS NULL")
                    if found:
                        providers.append(str(found))
                else:
                    found = _lookup(bind, "models", "name", name, "AND owner_id IS NULL")
                    if found:
                        models.append(str(found))
            if not (providers or models):
                if negate:
                    continue  # excludes nothing that exists
                return who, target, rest, False
            target = {
                "mode": "except" if negate else "only",
                "providers": providers,
                "models": models,
            }
        elif key in ("who", "target"):
            logger.warning("a routing rule's %s condition cannot be kept; it is dropped", key)
        else:
            rest.append(c)
    return who, target, rest, True


def _variants(conditions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The old conditions in the new keys; "this or that" becomes alternatives."""
    options: list[list[list[dict[str, Any]]]] = []
    for c in conditions:
        key, values, negate = c["key"], list(c.get("values") or []), bool(c.get("negate"))
        decide = c.get("decide")
        if key == "filetype":
            plain = [v for v in values if v != "scan"]
            scan = "scan" in values
            if negate:
                one: list[dict[str, Any]] = []
                if plain:
                    one.append({"key": "filetype", "values": plain, "negate": True})
                if scan:
                    one.append({"key": "scan", "values": [], "negate": True})
                options.append([one])
            else:
                alts: list[list[dict[str, Any]]] = []
                if plain:
                    alts.append([{"key": "filetype", "values": plain, "negate": False}])
                if scan:
                    alts.append([{"key": "scan", "values": [], "negate": False}])
                options.append(alts)
        elif key == "verdict":
            kept = [v for v in values if v in ("health", "ids")]
            if negate:
                options.append(
                    [[{"key": v, "values": [], "negate": True, "decide": decide or "patterns"} for v in kept]]
                )
            else:
                options.append(
                    [
                        [{"key": v, "values": [], "negate": False, "decide": decide or "patterns"}]
                        for v in kept
                    ]
                )
        else:
            options.append([[{**c, "values": values, "negate": negate}]])
    if not options:
        return [[]]
    return [list(itertools.chain.from_iterable(combo)) for combo in itertools.product(*options)]


def _convert_routing(bind: sa.Connection, table: Any, add_pipeline: Any) -> None:
    rows = bind.execute(sa.select(*table.c).order_by(table.c.kind, table.c.position)).all()
    shared: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    shared_made: dict[str, datetime] = {}
    own: list[tuple[str, Any, dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for row in rows:
        kind = str(row.kind)
        if kind not in KINDS:
            continue
        who, target, rest, live = _scope_of(bind, list(row.conditions or []))
        if not live:
            logger.warning("routing rule %s names nothing that exists; it is not carried over", row.id)
            continue
        variants = _variants(rest)
        converted: list[dict[str, Any]] = []
        for index, conditions in enumerate(variants):
            rule_name = row.name if index == 0 else f"{row.name} ({index + 1})"
            converted.append(
                {
                    "id": str(row.id) if index == 0 else str(uuid.uuid5(row.id, str(index))),
                    "name": rule_name[:120],
                    "enabled": bool(row.enabled),
                    "observe_only": bool(row.observe_only),
                    "conditions": conditions,
                    "action": _action(dict(row.action or {})),
                }
            )
        if who["mode"] == "all" and target["mode"] == "all":
            shared[kind].extend(converted)
            shared_made.setdefault(kind, row.created_at)
        else:
            own.append((kind, row, who, target, converted))
    for kind in KINDS:
        if shared[kind]:
            add_pipeline(
                kind,
                f"Routing rules ({kind})",
                {"mode": "all", "ids": []},
                {"mode": "all", "providers": [], "models": []},
                shared[kind],
                {"level": BASE[kind]},
                created_at=shared_made.get(kind),
            )
    for kind, row, who, target, converted in own:
        add_pipeline(
            kind,
            row.name,
            who,
            target,
            converted,
            {"level": BASE[kind]},
            created_at=row.created_at,
        )


def downgrade() -> None:
    """Back to the shapes of 0081 and 0080, **empty**: the conversion is one way.

    Policies, pipelines and users' own policies are not turned back into scoped
    rules (a pipeline can say what no single scoped rule can). The tables are
    restored so the older code starts; what was in them is lost.
    """
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_local", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_table(
        "privacy_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("observe_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("action", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_privacy_rules_kind_position", "privacy_rules", ["kind", "position"])
    op.create_table(
        "user_redaction_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("regex", sa.String(length=512), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "kind", "name", name="uq_user_redaction_rules_name"),
        sa.CheckConstraint(
            "(kind = 'pattern' AND regex IS NOT NULL) OR (kind = 'mode' AND regex IS NULL)",
            name="ck_user_redaction_rules_kind",
        ),
        sa.CheckConstraint("mode <> 'off'", name="ck_user_redaction_rules_not_off"),
    )
    op.create_index("ix_user_redaction_rules_user_id", "user_redaction_rules", ["user_id"])
    op.create_table(
        "redaction_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "scope",
            sa.Enum(
                "all",
                "provider",
                "model",
                "group",
                "user",
                "api_key",
                name="redaction_scope",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_redaction_rules_lookup", "redaction_rules", ["is_active", "scope", "scope_id"]
    )
    op.drop_table("privacy_settings")
    op.drop_index("ix_privacy_pipelines_kind", table_name="privacy_pipelines")
    op.drop_index("ix_privacy_pipelines_owner_id", table_name="privacy_pipelines")
    op.drop_table("privacy_pipelines")
    op.drop_index("ix_user_policies_user_id", table_name="user_policies")
    op.drop_table("user_policies")
    op.drop_table("redaction_policies")
    with op.batch_alter_table("detector_profiles", schema=None) as batch_op:
        batch_op.drop_column("user_available")
