"""The directory mirror and its sync runs (batch user sync, ADR 0088 draft).

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-24

Two new tables, nothing altered: `directory_entries` holds what a directory
last said about each person (filled by a pull adapter or by SCIM), and
`directory_sync_runs` what each application of it changed. Empty until a
provider is given a sync adapter, so the upgrade changes no behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "directory_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("identity_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255)),
        sa.Column("email", sa.String(320)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("groups", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("present", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("preassigned_groups", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_id", "external_id", name="uq_directory_entries_provider_ext"),
    )
    op.create_index("ix_directory_entries_provider_id", "directory_entries", ["provider_id"])
    op.create_index("ix_directory_entries_user_id", "directory_entries", ["user_id"])
    op.create_table(
        "directory_sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("identity_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("seen", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("linked", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("deactivated", sa.Integer(), nullable=False),
        sa.Column("reactivated", sa.Integer(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("started_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    op.create_index(
        "ix_directory_sync_runs_provider_started",
        "directory_sync_runs",
        ["provider_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_table("directory_sync_runs")
    op.drop_table("directory_entries")
