"""GUI-configured connections: email settings and identity providers

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-03

See ADR 0051.

Two configuration tables, both **seeded from the environment at startup** and
afterwards owned by the console. This consciously supersedes the boundary ADR
0048 drew ("connection is environment, policy is configuration"): the operator
wants the IdP and the mail server editable in the GUI, including more than one
identity provider.

* ``email_settings`` is a single row — there is one mail server per
  deployment. Empty means the environment's SMTP values stand (ADR 0049's
  fallback), which keeps the upgrade silent.
* ``identity_providers`` is a table proper, because "possibly more than one"
  was the request. Empty plus a disabled environment OIDC means no provider;
  the startup seed inserts the environment's provider as the first row, so an
  upgrading deployment keeps logging in unchanged. The client secret is
  encrypted at rest with the same box as upstream API keys (ADR 0027) — it is
  the credential that mints identities, which is exactly the thing a database
  dump should not hand over.

Group claim, userinfo toggle and the IdP→local group mappings move onto the
provider row: with more than one IdP, each directory names its groups
differently, so those were per-provider facts all along. Provisioning policy
(auto-provision, the unknown-user rule, admin groups) stays global in
``oidc_config`` — it answers "who may exist here", not "how does this IdP
speak".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_settings",
        # One row, by convention: id is pinned by the code that writes it.
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("smtp_host", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column("smtp_username", sa.String(length=255), nullable=False, server_default=""),
        # Encrypted with the deployment's secret key (ADR 0027); never rendered.
        sa.Column("smtp_password_encrypted", sa.Text(), nullable=True),
        sa.Column("smtp_from", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
    )

    op.create_table(
        "identity_providers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # The slug the login routes and the sign-in buttons use.
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default='["openid", "profile", "email"]'),
        sa.Column("groups_claim", sa.String(length=255), nullable=False, server_default="groups"),
        sa.Column("fetch_userinfo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # [[idp_group, local_group], ...] — this directory's names to ours.
        sa.Column("group_mappings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_identity_providers_name", "identity_providers", ["name"], unique=True)
    op.create_index("ix_identity_providers_issuer", "identity_providers", ["issuer"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_identity_providers_issuer", table_name="identity_providers")
    op.drop_index("ix_identity_providers_name", table_name="identity_providers")
    op.drop_table("identity_providers")
    op.drop_table("email_settings")
