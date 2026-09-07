"""Account linking: one person, a local password and a directory identity.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-07

Two changes, both additive (ADR 0056).

``user_identities`` holds the *additional* ways an issuer names a person.
``users`` keeps the ``(issuer, subject)`` it was created with, and nothing
about a linked account is rewritten — which is the point. ``issuer ==
"local"`` is read in eight places as "this account's door is a password", so
rewriting a linked user's issuer to the provider's would silently disable the
local login, the password reset, ``gateway passwd``, and the ``is_admin``
escape hatch that recovers a deployment whose directory is misconfigured.

``identity_providers.link_local_by_email`` is the switch, and it is
``false`` for every existing row on purpose: an upgrade must not change who
can sign in as whom. Turning it on is a decision an operator makes per
directory, in the console.

No unique index on ``users.email`` accompanies this. Linking matches on
``(issuer="local", subject=email)``, which is *already* unique — the local
door's address is the subject, not the profile email, and the profile email is
mutable and can be reassigned between people. Matching on a column with those
properties is how "linking" becomes "an email claim is a password".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        # The address the match was made on, as it was at the time. The
        # provider may report a different one later and `users.email` follows
        # it; what this link was justified by must not move with it.
        sa.Column("matched_email", sa.String(320), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One identity belongs to one person; one person has at most one
        # identity per directory. Two different confusions, two constraints.
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
        sa.UniqueConstraint("user_id", "issuer", name="uq_user_identities_user_issuer"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])

    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "link_local_by_email",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.drop_column("link_local_by_email")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
