"""Redaction visibility: users' own rules, and a log entry per request (ADR 0112).

Revision ID: 0080
Revises: 0079
Create Date: 2026-11-08

* ``user_redaction_rules``: a user's own redaction rules, which can only
  tighten the administrators' (a custom pattern, or a stricter mode for one
  entity type). One row per rule; unique per user and name within a kind;
  never ``off`` (a CHECK, beside the validation, so a row that loosens cannot
  exist even if the API is bypassed). Cascades with the user.
* ``usage_records.redaction_detail``: nullable JSON, the redaction log entry
  for the request (per type: count, mode, deciding rule, sources). It holds no
  value and no offset. Null on every existing row.

Plain DDL, so it runs on SQLite as it is; the column is added through
``batch_alter_table`` like 0079's.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("redaction_detail", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("redaction_detail")
    op.drop_index("ix_user_redaction_rules_user_id", table_name="user_redaction_rules")
    op.drop_table("user_redaction_rules")
