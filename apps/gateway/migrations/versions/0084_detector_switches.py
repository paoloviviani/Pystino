"""Per-detector switches on the append-only redaction config rows.

Revision ID: 0084
Revises: 0083
Create Date: 2026-11-09

The Detectors tab switches each detector on or off: the pattern recognisers
(``presidio_pattern_matching``, already there), and each NER engine, spaCy and
GLiNER. Null is "not decided": the row's older ``presidio_ner`` (or the
deployment default) applies, so existing rows behave exactly as before.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

#: Once listed as pattern formats (Presidio's spaCy recogniser advertises them), though no
#: pattern finds them and the stock models never emit them: a policy acting on them
#: protected nothing.
NEVER_DETECTED = ("AGE", "ID", "EMAIL")

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("redaction_config", sa.Column("spacy_enabled", sa.Boolean(), nullable=True))
    op.add_column("redaction_config", sa.Column("gliner_enabled", sa.Boolean(), nullable=True))
    _switch_off_never_detected_formats()


def _switch_off_never_detected_formats() -> None:
    """Keep AGE, ID and EMAIL under formats but off: kept and visible, never dropped.

    They never detected anything, so turning them off changes no protection; leaving them
    acting would make the request path refuse requests when a detector family is switched
    off, for a type no family finds.
    """
    bind = op.get_bind()
    policies = sa.table(
        "redaction_policies", sa.column("id", sa.Uuid()), sa.column("policy", sa.JSON())
    )
    for row in bind.execute(sa.select(policies.c.id, policies.c.policy)).all():
        policy = dict(row.policy or {})
        formats = policy.get("formats")
        if not isinstance(formats, dict):
            continue
        changed = [
            name
            for name, entry in formats.items()
            if str(name).upper() in NEVER_DETECTED
            and isinstance(entry, dict)
            and entry.get("mode", "anonymise_restore") != "off"
        ]
        if not changed:
            continue
        policy["formats"] = {
            name: ({**entry, "mode": "off"} if name in changed else entry)
            for name, entry in formats.items()
        }
        bind.execute(
            sa.update(policies).where(policies.c.id == row.id).values(policy=policy)
        )
        logger.warning(
            "0084: policy %s: %s were never detected by any recogniser and are now off",
            row.id,
            ", ".join(sorted(changed)),
        )


def downgrade() -> None:
    with op.batch_alter_table("redaction_config") as batch:
        batch.drop_column("gliner_enabled")
        batch.drop_column("spacy_enabled")
