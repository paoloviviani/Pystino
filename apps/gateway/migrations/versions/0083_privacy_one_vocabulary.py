"""One detection vocabulary: policies find items, conditions count them (ADR 0113, section 0c).

Revision ID: 0083
Revises: 0082
Create Date: 2026-11-09

#52 is unreleased, so this only ever sees rows made on a branch build, but it is
written as if it saw real ones: nothing is dropped silently.

* **Policies.** ``redaction_policies.policy`` is re-written in the two-group shape
  (``formats`` for built-in pattern formats, ``entities`` for NER types, plus the
  new ``custom_entities`` and ``keywords`` and an ``engine``). The split is the
  service's own: PERSON, LOCATION, NRP and ORGANIZATION are NER, every other name is
  a built-in format. ``engine`` is null (the deployment default), which is what
  every policy did before. Redaction output is unchanged (the contract test).
  ``redaction_policies.shared_with_users`` is added, false.
* **Profiles are gone.** ``detector_profiles`` and the ``profile_id`` columns on
  policies and user policies are dropped; the engine choice replaces them.
* **Classifiers.** ``privacy_classifiers`` (a model and a topic in plain words) is
  created, empty. Nothing is seeded.
* **Sharing.** Users could apply any administrator policy; now only one marked
  ``shared_with_users``. The policies users' pipelines already apply are marked, so
  nothing stops working.
* **Conditions.** The quick check is removed with its conditions:

  * ``ids`` becomes "finds at least 1 item" of a policy named "Identifiers
    (migrated)" that lists the built-in formats the quick check knew (fiscal code,
    VAT number, IBAN, card, US SSN, NHS number) as ``detect_only``. Same formats,
    now evaluated by the redaction service's recognisers.
  * ``health`` and ``lang`` have no equivalent (no list ships any more). A rule that
    used one is **kept, disabled**, with the condition removed and a note saying
    what it was; a rule whose action protects can then only widen (protect more)
    when an administrator turns it on, and a rule that protected nothing ("pass")
    is also made observe-only so enabling it cannot loosen anything. Every such
    rule is named in the migration log.

Plain DDL plus Python-side row conversion, so it runs on SQLite as on PostgreSQL. The
downgrade restores the schema only (see its docstring).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: The model-backed types of the redaction service; every other name is a pattern format.
NER_TYPES = frozenset({"PERSON", "LOCATION", "NRP", "ORGANIZATION"})
#: What the quick check's "contains IDs" looked for, as the service names them.
IDENTIFIER_FORMATS = (
    "IBAN_CODE",
    "CREDIT_CARD",
    "IT_FISCAL_CODE",
    "IT_VAT_CODE",
    "UK_NHS",
    "US_SSN",
)
IDS_POLICY = "Identifiers (migrated)"
REMOVED = {"health": "contains health data", "lang": "is written in a language"}
BASE = {"read": "default", "embed": "pass", "chat": "pass"}


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("redaction_policies", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("shared_with_users", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_table(
        "privacy_classifiers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.String(length=1000), server_default="", nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("name"),
    )

    _convert(bind)

    with op.batch_alter_table("redaction_policies", schema=None) as batch_op:
        batch_op.drop_column("profile_id")
    with op.batch_alter_table("user_policies", schema=None) as batch_op:
        batch_op.drop_column("profile_id")
    op.drop_table("detector_profiles")


# -- the conversion -----------------------------------------------------------------


def split_policy(raw: dict[str, Any]) -> dict[str, Any]:
    """A stored policy in the two-group shape. Already split: unchanged."""
    if "formats" in raw:
        return raw
    entities = raw.get("entities") or {}
    ner = {k: v for k, v in entities.items() if str(k).upper() in NER_TYPES}
    formats = {k: v for k, v in entities.items() if str(k).upper() not in NER_TYPES}
    return {
        **raw,
        "engine": raw.get("engine"),
        "formats": formats,
        "entities": ner,
        "custom_entities": raw.get("custom_entities") or [],
        "keywords": raw.get("keywords") or [],
    }


def convert_rules(
    rules: list[dict[str, Any]], ids_policy: str, *, pipeline: str, user: bool
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """The rules with the removed conditions converted: (rules, used the ids policy, log lines)."""
    out: list[dict[str, Any]] = []
    used_ids = False
    log: list[str] = []
    for rule in rules:
        conditions = list(rule.get("conditions") or [])
        kept: list[dict[str, Any]] = []
        removed: list[str] = []
        for condition in conditions:
            key = condition.get("key")
            if key == "ids":
                used_ids = True
                kept.append(
                    {
                        "key": "finds",
                        "values": [ids_policy],
                        "negate": bool(condition.get("negate")),
                        "count": 1,
                    }
                )
                log.append(
                    f"pipeline '{pipeline}', rule '{rule.get('name')}': 'IDs' became "
                    f"'finds at least 1 item' of the policy '{IDS_POLICY}'."
                )
            elif key in REMOVED:
                no = "does not have: " if condition.get("negate") else ""
                removed.append(f"{no}{REMOVED[key]} {', '.join(condition.get('values') or [])}".strip())
            else:
                kept.append({k: v for k, v in condition.items() if k != "decide"})
        action = dict(rule.get("action") or {})
        new = {**rule, "conditions": kept}
        new.pop("note", None)
        if removed:
            protective = action.get("level") not in (None, "pass", "default") or bool(
                action.get("redact_with")
            ) or bool(action.get("engine"))
            note = (
                "Migrated: this rule had the condition '"
                + "; ".join(removed)
                + "', which no longer exists (no list of topics ships any more), so the condition "
                "was removed and the rule switched off. "
            )
            if protective:
                note += "Turned on as it is it would apply to more requests than before: check it."
            else:
                note += (
                    "It only passed requests, so it is also observe-only: set what it should do "
                    "before turning it on."
                )
                new["observe_only"] = True
            new["enabled"] = False
            new["note"] = note
            log.append(
                f"pipeline '{pipeline}', rule '{rule.get('name')}': removed condition "
                f"'{'; '.join(removed)}' (no equivalent); the rule is kept, switched off"
                + ("" if protective else " and observe-only")
                + "."
            )
        out.append(new)
    return out, used_ids, log


def _convert(bind: sa.Connection) -> None:
    policies = sa.table(
        "redaction_policies",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("policy", sa.JSON()),
        sa.column("is_active", sa.Boolean()),
        sa.column("shared_with_users", sa.Boolean()),
        sa.column("reason", sa.String()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    pipelines = sa.table(
        "privacy_pipelines",
        sa.column("id", sa.Uuid()),
        sa.column("owner_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("rules", sa.JSON()),
        sa.column("otherwise", sa.JSON()),
    )
    for row in bind.execute(sa.select(policies.c.id, policies.c.policy)).all():
        converted = split_policy(dict(row.policy or {}))
        if converted != (row.policy or {}):
            bind.execute(
                policies.update().where(policies.c.id == row.id).values(policy=converted)
            )

    # Users could apply any administrator policy before; now only a shared one. So the
    # policies users' pipelines already apply are shared, and behaviour is unchanged.
    shared: set[uuid.UUID] = set()
    for row in bind.execute(
        sa.select(pipelines.c.rules, pipelines.c.otherwise).where(pipelines.c.owner_id.is_not(None))
    ).all():
        actions = [dict(r.get("action") or {}) for r in row.rules or []] + [dict(row.otherwise or {})]
        for action in actions:
            if action.get("redact_with"):
                shared.add(uuid.UUID(str(action["redact_with"])))
    for policy_id in shared:
        bind.execute(
            policies.update().where(policies.c.id == policy_id).values(shared_with_users=True)
        )

    ids_policy = uuid.uuid4()
    used_by_admin = used_by_user = False
    log: list[str] = []
    for row in bind.execute(
        sa.select(pipelines.c.id, pipelines.c.owner_id, pipelines.c.name, pipelines.c.rules)
    ).all():
        rules = list(row.rules or [])
        if not any(
            c.get("key") in ("ids", "health", "lang") or "decide" in c
            for rule in rules
            for c in rule.get("conditions") or []
        ):
            continue
        new_rules, used, lines = convert_rules(
            rules, str(ids_policy), pipeline=row.name, user=row.owner_id is not None
        )
        log.extend(lines)
        if used:
            if row.owner_id is None:
                used_by_admin = True
            else:
                used_by_user = True
        bind.execute(pipelines.update().where(pipelines.c.id == row.id).values(rules=new_rules))

    if used_by_admin or used_by_user:
        now = datetime.now(UTC)
        bind.execute(
            policies.insert().values(
                id=ids_policy,
                name=IDS_POLICY,
                policy={
                    "engine": None,
                    "default_mode": "off",
                    "formats": {name: {"mode": "detect_only"} for name in IDENTIFIER_FORMATS},
                    "entities": {},
                    "custom_entities": [],
                    "patterns": [],
                    "keywords": [],
                    "allow_list": [],
                },
                is_active=True,
                shared_with_users=used_by_user,
                reason="Made by the migration: what the removed 'IDs' condition looked for.",
                created_by=None,
                created_at=now,
                updated_at=now,
            )
        )
        log.append(
            f"made the policy '{IDS_POLICY}' (detect only: {', '.join(IDENTIFIER_FORMATS)})"
            + (" and shared it with users." if used_by_user else ".")
        )
    for line in log:
        logger.warning("privacy migration 0083: %s", line)


def downgrade() -> None:
    """Back to the profiles schema, so the chain stays walkable. The rows are not
    converted back: migrations only go forward, and the earlier downgrades only need
    the tables and columns to exist."""
    op.create_table(
        "detector_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=500), server_default="", nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("fallback", sa.String(length=16), server_default="refuse", nullable=False),
        sa.Column("fallback_profile_id", sa.Uuid(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("user_available", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["fallback_profile_id"], ["detector_profiles.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("name"),
    )
    with op.batch_alter_table("redaction_policies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("profile_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_redaction_policies_profile", "detector_profiles", ["profile_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("user_policies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("profile_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_user_policies_profile", "detector_profiles", ["profile_id"], ["id"], ondelete="SET NULL"
        )

    op.drop_table("privacy_classifiers")
    with op.batch_alter_table("redaction_policies", schema=None) as batch_op:
        batch_op.drop_column("shared_with_users")
