"""inference providers, per-user model access

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-15

Turns the single environment-configured upstream into provider records
(docs/adr/0027-inference-providers.md).

The upgrade must be invisible to a running deployment, so it creates a
``default`` provider from the existing ``GATEWAY_UPSTREAM__*`` values and points
every existing model at it. After this, routing reads the provider row and the
environment is only the bootstrap.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Matches gateway.config.UpstreamSettings.base_url, so a deployment that never
# set the variable still lands on the endpoint it has been using.
_DEFAULT_BASE_URL = "https://api.cortecs.ai/v1"


def _seed_provider() -> dict[str, object]:
    """The `default` provider, from the environment this gateway already uses.

    Read straight from ``os.environ`` rather than through Settings: a migration
    must not import application configuration, which validates far more than
    this needs and would fail the upgrade for an unrelated reason.
    """
    base_url = (os.environ.get("GATEWAY_UPSTREAM__BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
    api_key = os.environ.get("GATEWAY_UPSTREAM__API_KEY") or ""

    encrypted = ""
    hint = ""
    if api_key:
        # Encrypting here means the key survives the upgrade and nobody has to
        # re-enter it. Without a secret key configured the credential cannot be
        # stored, so the row is created without one and the operator is told —
        # far better than an upgrade that fails, or one that stores plaintext.
        try:
            from gateway.secrets import SecretBox, hint_for

            box = SecretBox((os.environ.get("GATEWAY_SECRET_KEY") or "").split(","))
            if box.enabled:
                encrypted = box.encrypt(api_key)
                hint = hint_for(api_key)
            else:
                logger.warning(
                    "GATEWAY_SECRET_KEY is not set, so the existing upstream API key "
                    "could not be migrated into the providers table. Set it and "
                    "re-enter the key for the 'default' provider in the console."
                )
        except Exception:
            logger.warning(
                "could not encrypt the existing upstream API key during migration; "
                "re-enter it for the 'default' provider in the console",
                exc_info=True,
            )

    return {
        "id": uuid.uuid4(),
        "name": "default",
        "description": "Created automatically from GATEWAY_UPSTREAM__* at upgrade.",
        "base_url": base_url,
        "api_key_encrypted": encrypted,
        "api_key_hint": hint,
        "extra_headers": {},
        "is_active": True,
    }


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("api_key_hint", sa.String(length=64), nullable=False),
        sa.Column("extra_headers", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_providers_name"), ["name"], unique=True)

    op.create_table(
        "user_model_access",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "model_id"),
    )

    # The seed row goes in before the column that references it, so the backfill
    # below has something to point at.
    seed = _seed_provider()
    now = sa.func.now()
    providers = sa.table(
        "providers",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("base_url", sa.String()),
        sa.column("api_key_encrypted", sa.Text()),
        sa.column("api_key_hint", sa.String()),
        sa.column("extra_headers", sa.JSON()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(providers.insert().values(**seed, created_at=now, updated_at=now))

    with op.batch_alter_table("models", schema=None) as batch_op:
        # Nullable first: existing rows have no value yet, and a NOT NULL column
        # cannot be added to a populated table without one.
        batch_op.add_column(sa.Column("provider_id", sa.Uuid(), nullable=True))

    models = sa.table("models", sa.column("provider_id", sa.Uuid()))
    op.execute(models.update().values(provider_id=seed["id"]))

    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.alter_column("provider_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_index(batch_op.f("ix_models_provider_id"), ["provider_id"], unique=False)
        # RESTRICT, not CASCADE: deleting a provider that still serves models
        # would leave historical spend pointing at a model nobody can explain.
        batch_op.create_foreign_key(
            "fk_models_provider_id", "providers", ["provider_id"], ["id"], ondelete="RESTRICT"
        )
        # Superseded by provider_id. It was written by the importer and read by
        # nothing; keeping it beside the FK would give a display name two sources
        # of truth.
        batch_op.drop_column("provider")


def downgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provider", sa.String(length=64), nullable=False, server_default="default")
        )
        batch_op.drop_constraint("fk_models_provider_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_models_provider_id"))
        batch_op.drop_column("provider_id")

    op.drop_table("user_model_access")

    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_providers_name"))
    op.drop_table("providers")
