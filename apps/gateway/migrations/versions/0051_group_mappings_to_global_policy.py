"""Group mappings move to the global policy (ADR 0093 §3.4), closing R9.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-26

The provider row's ``group_mappings`` is not dropped — a directory sync
adapter still keeps its own copy for its own reconciliation, unrelated to
this migration — but it stops being *read* for the provisioning policy and
the admin claim rule (``gw/deps.py``, ``gw/routers/auth.py``): those now fold
only ``oidc_config``, the global policy the console already edits.

Only the ``default`` row's mappings move, and only once: if the policy
already has some (an administrator may have started using the console's
identity policy screen for this before upgrading), nothing is touched. Any
other row's mappings are not moved — there is only ever one active provider
now (ADR 0093 §2), so another row's mappings have no home to fold into — and
are printed for the operator to notice, the same way migration 0010 surfaces
a value it cannot carry forward.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

_REASON = "moved from the identity provider row (ADR 0093)"


def _mappings_of(value: object) -> list[list[str]] | None:
    """A JSON column's value, whatever shape the driver handed back.

    Raw ``text()`` SQL bypasses the ORM's JSON type decoder, and asyncpg does
    not decode ``json``/``jsonb`` to Python objects on its own the way
    SQLAlchemy's typed columns arrange for it to — so this may already be a
    list, or still the JSON string SQLite always hands back.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return value if value else None


def upgrade() -> None:
    connection = op.get_bind()

    newest = connection.execute(
        sa.text("SELECT group_mappings FROM oidc_config ORDER BY created_at DESC LIMIT 1")
    ).first()
    policy_has_mappings = newest is not None and _mappings_of(newest[0]) is not None

    default_row = connection.execute(
        sa.text("SELECT group_mappings FROM identity_providers WHERE name = 'default'")
    ).first()
    default_mappings = _mappings_of(default_row[0]) if default_row is not None else None

    if not policy_has_mappings and default_mappings:
        connection.execute(
            sa.text(
                "INSERT INTO oidc_config (id, group_mappings, reason, created_at) "
                "VALUES (:id, :mappings, :reason, :created_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "mappings": json.dumps(default_mappings),
                "reason": _REASON,
                "created_at": datetime.now(UTC),
            },
        )

    others = connection.execute(
        sa.text("SELECT name, group_mappings FROM identity_providers WHERE name != 'default'")
    ).all()
    for name, raw_mappings in others:
        mappings = _mappings_of(raw_mappings)
        if mappings and mappings != default_mappings:
            print(
                f"  migration 0051: identity provider {name!r} has its own group "
                f"mappings ({mappings!r}), not moved to the global policy — only the "
                "active provider's did. It is disabled (ADR 0093 §2); its mappings "
                "are still on its row if this one is ever re-enabled by issuer."
            )


def downgrade() -> None:
    # The inserted policy row is not deleted: oidc_config is append-only by
    # design (ADR 0048), and a downgrade that deleted a row would violate
    # that on its way past, for a table nothing below 0051 reads differently.
    pass
