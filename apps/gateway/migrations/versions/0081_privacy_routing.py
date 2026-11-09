"""Privacy routing: rules, detector profiles, and a local flag on providers (ADR 0113).

Revision ID: 0081
Revises: 0080
Create Date: 2026-11-08

* ``privacy_rules``: the administrator's ordered routing rules per kind of
  request (read, embed, chat). Conditions and the action are JSON.
* ``detector_profiles``: named detectors with a per-profile fallback; the
  deployment default "Fast" (the current Presidio detector) is seeded here so
  every deployment has one.
* ``providers.is_local``: an administrator's statement that an endpoint runs on
  this deployment's own infrastructure. False on every existing row.

Plain DDL plus one INSERT; the column goes through ``batch_alter_table`` so it
runs on SQLite as it does on PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["fallback_profile_id"], ["detector_profiles.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("name"),
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

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_local", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    now = datetime.now(UTC)
    profiles = sa.table(
        "detector_profiles",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("engine", sa.String()),
        sa.column("labels", sa.JSON()),
        sa.column("fallback", sa.String()),
        sa.column("is_default", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        profiles,
        [
            {
                "id": uuid.uuid4(),
                "name": "Fast",
                "description": "The detector this deployment already uses: patterns and, "
                "where installed, language models. Instant for verdicts.",
                "engine": "presidio",
                "labels": [],
                "fallback": "refuse",
                "is_default": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_column("is_local")
    op.drop_index("ix_privacy_rules_kind_position", table_name="privacy_rules")
    op.drop_table("privacy_rules")
    op.drop_table("detector_profiles")
