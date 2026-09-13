"""Search tiers collapse to one grant anchor per backend.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-13

ADR 0071. The search route became a passthrough: the caller speaks the
vendor's own API and the gateway forwards the body verbatim, so a "model" on
a search provider — once a backend at a tier, with the depth in
``upstream_model`` — has nothing left to be. What a caller is granted is the
*backend*, and what carries that grant is one model row per search provider,
named after it.

The migration folds the tier rows into those anchors: one anchor per
provider, the union of every tier's group grants, the tiers deleted. Personal
(``user_model_access``) grants on tiers move the same way; a user who held
two tiers of one backend holds the backend once.

Downgrade refuses. The tier rows carried ``upstream_model`` depths that a
passthrough never reads again; recreating them would present tiers as models
— the exact confusion this migration removes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    providers = conn.execute(
        sa.text("SELECT id, name FROM providers WHERE kind = 'search'")
    ).fetchall()
    for provider_id, provider_name in providers:
        # The anchor, if an earlier release already made one; otherwise here.
        anchor = conn.execute(
            sa.text(
                "SELECT id FROM models WHERE provider_id = :pid AND kind = 'search' "
                "AND upstream_model = 'search'"
            ),
            {"pid": provider_id},
        ).scalar()
        if anchor is None:
            anchor = conn.execute(
                sa.text(
                    "INSERT INTO models (id, name, upstream_model, provider_id, kind, "
                    "input_modalities, output_modalities, supported_features, created_at, "
                    "updated_at) "
                    "VALUES (gen_random_uuid(), :name, 'search', :pid, 'search', "
                    "'[\"text\"]', '[\"text\"]', '[]', now(), now()) RETURNING id"
                ),
                {"name": provider_name, "pid": provider_id},
            ).scalar()
        assert anchor is not None

        tiers = conn.execute(
            sa.text(
                "SELECT id FROM models WHERE provider_id = :pid AND kind = 'search' "
                "AND id != :anchor"
            ),
            {"pid": provider_id, "anchor": anchor},
        ).fetchall()

        for (tier_id,) in tiers:
            # Group grants: union onto the anchor, skipping pairs it already has.
            conn.execute(
                sa.text(
                    "INSERT INTO group_model_access (group_id, model_id, created_at) "
                    "SELECT g.group_id, :anchor, COALESCE(MAX(g.created_at), now()) "
                    "FROM group_model_access g WHERE g.model_id = :tier "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM group_model_access x "
                    "  WHERE x.group_id = g.group_id AND x.model_id = :anchor) "
                    "GROUP BY g.group_id"
                ),
                {"tier": tier_id, "anchor": anchor},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO user_model_access (user_id, model_id, created_at) "
                    "SELECT u.user_id, :anchor, COALESCE(MAX(u.created_at), now()) "
                    "FROM user_model_access u WHERE u.model_id = :tier "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM user_model_access x "
                    "  WHERE x.user_id = u.user_id AND x.model_id = :anchor) "
                    "GROUP BY u.user_id"
                ),
                {"tier": tier_id, "anchor": anchor},
            )
            # Historical usage keeps its denormalised model_name, so deleting
            # the row loses no readable spend.
            conn.execute(sa.text("DELETE FROM models WHERE id = :tier"), {"tier": tier_id})


def downgrade() -> None:
    raise RuntimeError(
        "ADR 0071: search tiers are body fields now, not models. Recreating "
        "tier rows would present depths as models — the confusion this "
        "migration removes."
    )
