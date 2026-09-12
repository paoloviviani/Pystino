"""Agents belong to the chat.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-12

ADR 0067. Agents leave the gateway: they were a served model with a prompt,
tools and knowledge bases attached, addressed as ``agent:<name>`` so any
OpenAI-compatible client could pick one from ``/v1/models``. The decision they
move on reverses the premise — agents are a chat-side construction the chat
assembles before the call, owned by the user who made them, shared with
nobody — so the tables go, and the wire name ``agent:<name>`` means nothing
here from now on.

Order matters in the upgrade. ``resource_shares`` rows of kind ``agent`` are
deleted before the table drop: they carry no foreign key into ``agents``
(shares are by id on purpose, and a share may outlive... well, not here), but
leaving grants that name nothing is exactly the "acl row pointing at nothing"
the join tables exist to prevent. ``agent_knowledge_bases`` goes before
``agents`` because its foreign key points at the table being dropped.

Downgrade refuses rather than rebuilding. The columns, constraints and
defaults of the 0029 shape are recoverable from that migration, but the
*agent definitions the chat is now the owner of* are not here to restore:
they live in the chat's store, and a downgrade that silently created empty
shells for them would be worse than one that stops and says what it would
lose.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("delete from resource_shares where resource_kind = 'agent'")
    op.drop_table("agent_knowledge_bases")
    op.drop_table("agents")


def downgrade() -> None:
    raise RuntimeError(
        "agents now live in the chat (ADR 0067): the definitions this database "
        "once served are the chat's to own now, and recreating the 0029 tables "
        "empty would leave every agent a shell with no prompt behind it. This "
        "is not a downgrade anyone asked for."
    )
