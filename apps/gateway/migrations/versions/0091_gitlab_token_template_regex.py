"""Stored GITLAB_TOKEN patterns match the whole routable token.

Revision ID: 0091
Revises: 0090
Create Date: 2026-11-25

The console's GITLAB_TOKEN template was ``glpat-[A-Za-z0-9_-]{20,}``. GitLab's routable
tokens look like ``glpat-<payload>.01.<suffix>``; the old expression stopped at the first
dot and left the tail of the token in the redacted text. The template now matches the whole
token and every documented GitLab prefix. A template is copied into the policy when it is
added, so already-stored patterns keep the old expression until this migration rewrites them.

Only an entry whose regex equals the old template string exactly is rewritten (in an
administrator policy, ``redaction_policies.policy["patterns"]``, and in a user's own policy,
``user_policies.rules`` entries of kind ``pattern``); an expression somebody edited is left
alone, whatever its name. The downgrade restores the old expression on entries holding the
new one exactly.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

OLD = "glpat-[A-Za-z0-9_-]{20,}"
NEW = (
    "gl(pat|dt|rt|ptt|ft|imt|oas|cbt|soat|ffct|agent)-[A-Za-z0-9_-]{20,}(\\.[A-Za-z0-9_-]+)*"
)


def swap(items: Any, old: str, new: str) -> tuple[Any, int]:
    """The list with every entry whose regex is exactly ``old`` rewritten, and how many."""
    if not isinstance(items, list):
        return items, 0
    out: list[Any] = []
    changed = 0
    for item in items:
        if isinstance(item, dict) and item.get("regex") == old:
            item = {**item, "regex": new}
            changed += 1
        out.append(item)
    return out, changed


def _rewrite(old: str, new: str) -> None:
    bind = op.get_bind()
    policies = sa.table("redaction_policies", sa.column("id", sa.Uuid()), sa.column("policy", sa.JSON()))
    for row in bind.execute(sa.select(policies.c.id, policies.c.policy)).all():
        policy = row.policy if isinstance(row.policy, dict) else {}
        patterns, count = swap(policy.get("patterns"), old, new)
        if count:
            bind.execute(
                policies.update()
                .where(policies.c.id == row.id)
                .values(policy={**policy, "patterns": patterns})
            )
            logger.info("gitlab token pattern 0091: %d entry(ies) in policy %s", count, row.id)
    own = sa.table("user_policies", sa.column("id", sa.Uuid()), sa.column("rules", sa.JSON()))
    for row in bind.execute(sa.select(own.c.id, own.c.rules)).all():
        rules, count = swap(row.rules, old, new)
        if count:
            bind.execute(own.update().where(own.c.id == row.id).values(rules=rules))
            logger.info("gitlab token pattern 0091: %d entry(ies) in user policy %s", count, row.id)


def upgrade() -> None:
    _rewrite(OLD, NEW)


def downgrade() -> None:
    _rewrite(NEW, OLD)
