"""Agents: a served model with a prompt, tools and knowledge bases attached.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-09

Two tables (ADR 0062).

**An agent is addressed as a model**, under the wire name ``agent:<name>``, and
that is why it is here rather than in the chat application: any
OpenAI-compatible client can use one by choosing it from ``/v1/models``, with
no code that knows agents exist. A bespoke ``/v1/agents/{id}/chat`` would have
given agents to one client and to nothing else, ever.

The prefix is not stored. ``agents.name`` holds ``research`` and the wire name
is ``agent:research``, because storing the prefix would mean two spellings of
one identity and a lookup that has to strip before it compares.

**An agent is not a billing entity.** It carries no price and no provider: the
request is billed against ``model_id`` at that model's prices, exactly as a
direct call would be. What an agent changes is the *payload* — a system prompt,
some tools, some retrieved passages — and each of those lands in the prompt
token count before the reservation is taken, so an agent cannot become a way to
spend money that nothing accounts for.

Two foreign-key choices worth reading before changing them:

* ``agents.model_id`` is **RESTRICT**. An agent whose model has gone cannot
  serve a request, and finding that out at the first call is worse than finding
  it out at the deletion. Models deactivate rather than delete, so it does not
  fire in practice — it is a statement that deleting one would be a bug.
* ``agent_knowledge_bases`` is a **join table, not a JSON array of ids**, so
  that ``ON DELETE CASCADE`` makes "deleting a base leaves an agent pointing at
  nothing" impossible rather than merely unlikely.

What is deliberately **not** here: no ACL columns. Agents are shared through
``resource_shares`` with kind ``agent``, the same table and the same predicate
as knowledge bases, because a second sharing mechanism is how a deployment ends
up with somebody who can reach a resource through one door and not the other.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        # Without the `agent:` prefix — see the module docstring.
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "billing_group_id",
            sa.Uuid(),
            sa.ForeignKey("groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "model_id",
            sa.Uuid(),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Prepended as a system message rather than replacing the caller's: a
        # caller's system message is their instruction about their own task,
        # and dropping it makes an agent silently hostile to its client.
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        # The provider's shape, not ours. Normalising it into columns would
        # mean re-encoding a schema that changes whenever a provider adds a
        # field, which is what `extra="allow"` exists to avoid.
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("generation", sa.JSON(), nullable=False),
        sa.Column("retrieval_limit", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("retrieval_min_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agents_name", "agents", ["name"], unique=True)
    op.create_index("ix_agents_owner", "agents", ["owner_user_id"])
    op.create_index("ix_agents_created", "agents", ["created_at"])

    op.create_table(
        "agent_knowledge_bases",
        sa.Column(
            "agent_id",
            sa.Uuid(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_base_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "knowledge_base_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_knowledge_bases")
    op.drop_index("ix_agents_created", table_name="agents")
    op.drop_index("ix_agents_owner", table_name="agents")
    op.drop_index("ix_agents_name", table_name="agents")
    op.drop_table("agents")
