"""Read (extract) pipeline rules no longer redact.

Revision ID: 0088
Revises: 0087
Create Date: 2026-11-20

A read pipeline decides how a file is read (which reader, or refuse). It no longer hides
anything in the text it returns; chat and embedding pipelines still redact that text before
it reaches a model or an index. A stored extract rule that carried policies, a mode other
than ``off`` or overrides loses those three parts and keeps the rest (name, conditions,
action). Each such rule is logged (``read rules 0088: ...``) and gets one
``admin_audit_events`` row (actor "system migration", action
``privacy_pipeline.read_redaction_dropped``) holding the rule id and what was dropped.

The downgrade restores nothing (the dropped parts are not kept anywhere else than the audit
rows). It refuses when such audit rows exist, unless ``PYSTINO_ALLOW_LOSSY_DOWNGRADE=1``.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

ACTION = "privacy_pipeline.read_redaction_dropped"


def strip_rule(rule: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The rule without redaction, and what was dropped (empty when nothing was)."""
    dropped: dict[str, Any] = {}
    if rule.get("policies"):
        dropped["policies"] = [str(p) for p in rule["policies"]]
    if rule.get("mode") not in (None, "", "off"):
        dropped["mode"] = rule["mode"]
    if rule.get("overrides"):
        dropped["overrides"] = dict(rule["overrides"])
    if not dropped:
        return rule, {}
    return {**rule, "policies": [], "mode": "off", "overrides": {}}, dropped


def upgrade() -> None:
    bind = op.get_bind()
    pipes = sa.table(
        "privacy_pipelines",
        sa.column("id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("name", sa.String()),
        sa.column("rules", sa.JSON()),
    )
    names: dict[str, str] = {}
    for table, column in (("redaction_policies", "name"), ("user_policies", "name")):
        t = sa.table(table, sa.column("id", sa.Uuid()), sa.column(column, sa.String()))
        names.update({str(r[0]): str(r[1]) for r in bind.execute(sa.select(t.c.id, t.c[column]))})
    audit = sa.table(
        "admin_audit_events",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("actor_label", sa.String()),
        sa.column("action", sa.String()),
        sa.column("target_type", sa.String()),
        sa.column("target_id", sa.String()),
        sa.column("target_label", sa.String()),
        sa.column("changes", sa.JSON()),
    )
    for row in bind.execute(
        sa.select(pipes.c.id, pipes.c.name, pipes.c.rules).where(pipes.c.kind == "extract")
    ).all():
        rules = [dict(r) for r in (row.rules or [])]
        changed = False
        for index, rule in enumerate(rules):
            clean, dropped = strip_rule(rule)
            if not dropped:
                continue
            changed = True
            rules[index] = clean
            policy_names = [names.get(p, "a policy that no longer exists") for p in dropped.get("policies", [])]
            logger.warning(
                "read rules 0088: rule '%s' (%s) of read pipeline '%s' lost its redaction: "
                "policies %s, mode %s, overrides %s",
                rule.get("name"),
                rule.get("id"),
                row.name,
                policy_names or "none",
                dropped.get("mode", "off"),
                dropped.get("overrides") or "none",
            )
            bind.execute(
                audit.insert().values(
                    id=uuid.uuid4(),
                    created_at=datetime.now(UTC),
                    actor_label="system migration",
                    action=ACTION,
                    target_type="privacy_pipeline",
                    target_id=str(row.id),
                    target_label=str(row.name)[:320],
                    changes={
                        "rule_id": str(rule.get("id")),
                        "rule": rule.get("name"),
                        "dropped": {**dropped, "policy_names": policy_names},
                    },
                )
            )
        if changed:
            bind.execute(pipes.update().where(pipes.c.id == row.id).values(rules=rules))


def downgrade() -> None:
    """Nothing is restored: the older gateway simply finds read rules without redaction."""
    bind = op.get_bind()
    audit = sa.table("admin_audit_events", sa.column("action", sa.String()))
    count = bind.execute(
        sa.select(sa.func.count()).select_from(audit).where(audit.c.action == ACTION)
    ).scalar_one()
    if count and os.environ.get("PYSTINO_ALLOW_LOSSY_DOWNGRADE") != "1":
        raise RuntimeError(
            f"Downgrading 0088 restores nothing: {count} read rule(s) lost their 'Redact with' "
            "when it was applied (see the admin audit entries 'privacy_pipeline."
            "read_redaction_dropped'). Set PYSTINO_ALLOW_LOSSY_DOWNGRADE=1 to downgrade anyway."
        )
