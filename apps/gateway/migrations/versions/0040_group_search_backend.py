"""A billing group's search provider, for the unified search route.

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-14

``groups.search_model_id`` names the one search backend a group searches
through on ``POST /v1/search`` — the policy that makes "one endpoint per
group" a server-side guarantee rather than a client convention. Nullable
because no policy is the honest state: a group without one cannot use the
unified route at all, instead of silently searching through whatever backend
happened to be granted.

A foreign key to the backend's anchor row rather than a name, so renaming a
model cannot orphan a policy. ``ON DELETE SET NULL`` rather than RESTRICT:
deleting the model still clears the policy, but the admin route refuses the
deletion with a 409 first — the database default is the backstop for the
paths that do not go through that route, not the enforcement.

Numbered 0040 rather than 0039 because 0039 is taken — on a parallel branch
whose content is already stamped on deployments that ran it. This revision
layers on top of whatever 0039 holds, and touches only its own column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "groups",
        sa.Column(
            "search_model_id",
            sa.Uuid(),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("groups", "search_model_id")
