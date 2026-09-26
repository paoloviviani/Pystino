"""deployment_state: the bootstrap admin door stops re-arming (ADR 0093 §5.3).

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-26

`promote_bootstrap_admin` used to fire whenever no *active* administrator
existed, so deactivating every admin quietly reopened it — a hidden recovery
path, and just as much a hidden takeover path if the configured bootstrap
address were ever reachable by someone other than the operator (review
correction 2, R1). The new gate is this table's one row: has the deployment
*ever* had an administrator, through any door. Once true, it stays true.

The backfill treats an existing deployment as already past that point if any
``is_admin`` row exists at all, active or not — an inactive admin already
proved the door has been used once, and re-arming it for such a deployment
would be exactly the bug this closes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployment_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bootstrap_admin_consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    connection = op.get_bind()
    has_admin = (
        connection.execute(sa.text("SELECT 1 FROM users WHERE is_admin LIMIT 1")).first()
        is not None
    )
    connection.execute(
        sa.text(
            "INSERT INTO deployment_state (id, bootstrap_admin_consumed_at) "
            "VALUES (1, :consumed_at)"
        ),
        {"consumed_at": datetime.now(UTC) if has_admin else None},
    )


def downgrade() -> None:
    op.drop_table("deployment_state")
