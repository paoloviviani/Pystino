"""Resolving ``agent:<name>`` to a model, a prompt, some tools and some passages.

The one idea here: **an agent is addressed as a model**. A caller sends
``model: "agent:research"`` to ``/v1/chat/completions`` and gets the underlying
model with a system prompt prepended, the agent's tools merged in, its sampling
defaults filled where the request said nothing, and — if it has knowledge bases
attached — passages retrieved and put in front of the conversation.

That means every OpenAI-compatible client can use an agent by picking it from
``/v1/models``, without a line of code that knows agents exist (ADR 0062). The
alternative, a bespoke endpoint, would have given agents to the chat and to
nothing else.

**Where this runs matters.** `chat.py` calls it *before* redaction, and that
ordering is load-bearing in both directions. The system prompt and the
retrieved passages are text heading for a provider, so they must pass through
the redaction layer like anything else — putting agent resolution after it
would send a knowledge base's contents out unscreened. And the injected text
has to be in the messages before the prompt tokens are counted, or the
reservation is taken against a payload smaller than the one actually sent.

**What is deliberately not merged.** A caller's own ``system`` message is kept
and the agent's goes *in front of it*, rather than replacing it: the caller's
message is their instruction about their own task, and dropping it would make
an agent silently hostile to the client that chose it. Sampling parameters the
caller set are left alone for the same reason — an agent supplies defaults, not
overrides.

**Access is re-checked on every request**, never trusted from the attachment.
An owner may attach a base they can reach and later lose that access; an agent
that kept retrieving would be a way to read a document after being cut off from
it. `usable_bases` is where that check lives, and it is why retrieval takes a
session.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from gateway import sharing
from gateway.deps import Principal
from gateway.models import Agent, KnowledgeBase, ModelDef, ResourceKind, ShareRole

logger = logging.getLogger(__name__)

#: The namespace that makes an agent addressable as a model. A prefix rather
#: than a flat shared namespace so that resolution is a string test before any
#: query — the common case, a plain model name, costs nothing — and so that
#: "why is this model adding a system prompt" is answerable from the name.
PREFIX = "agent:"

#: How the retrieved passages are introduced to the model. Worth being explicit
#: about two things in the wording. It tells the model the material may be
#: irrelevant, because a retrieval that found nothing useful still returns its
#: nearest neighbours and a model told "here are the facts" will use them. And
#: it asks for the source name in the answer, which is the cheapest form of
#: citation available without a second round trip.
CONTEXT_PREAMBLE = (
    "The following passages were retrieved from the knowledge bases attached to "
    "this assistant. They may or may not be relevant to the question. Use them "
    "where they are, name the source you used, and say plainly when they do not "
    "answer the question rather than guessing from them."
)


def is_agent_name(name: str) -> bool:
    return name.startswith(PREFIX)


def bare_name(name: str) -> str:
    return name[len(PREFIX) :] if is_agent_name(name) else name


def wire_name(agent: Agent) -> str:
    return f"{PREFIX}{agent.name}"


@dataclass(slots=True)
class ResolvedAgent:
    """An agent, its model, and the bases this caller may actually retrieve from."""

    agent: Agent
    model: ModelDef
    bases: list[KnowledgeBase] = field(default_factory=list)


async def resolve(
    session: AsyncSession, name: str, *, principal: Principal
) -> ResolvedAgent | None:
    """The agent this caller may use, or None when the name is not an agent.

    None rather than an exception for a name without the prefix, so the caller
    can fall through to ordinary model resolution with no branch of its own.
    A name *with* the prefix that resolves to nothing this caller may reach
    raises the same 404 a model would — which is deliberate: whose agents exist
    is not this caller's business, and a 403 would confirm the name is real.
    """
    if not is_agent_name(name):
        return None

    from gateway.errors import ModelNotFoundError

    result = await session.execute(
        select(Agent)
        .where(Agent.name == bare_name(name), Agent.is_active.is_(True))
        .options(
            joinedload(Agent.model).selectinload(ModelDef.prices),
            joinedload(Agent.model).joinedload(ModelDef.provider),
            selectinload(Agent.knowledge_bases),
        )
    )
    agent = result.unique().scalars().first()
    if agent is None:
        raise ModelNotFoundError(
            f"The model {name!r} does not exist or you do not have access to it."
        )

    if not await sharing.may_reach(
        session,
        kind=ResourceKind.AGENT,
        resource_id=agent.id,
        owner_user_id=agent.owner_user_id,
        user_id=principal.user.id,
    ):
        raise ModelNotFoundError(
            f"The model {name!r} does not exist or you do not have access to it."
        )

    # The *model* is checked separately and on purpose. Being given an agent is
    # not being given its model: an agent whose model this caller may not use
    # would otherwise be a way around `access.py`, which is the one predicate
    # this gateway has for that question.
    from gateway.access import accessible_model_by_name

    allowed = (
        (
            await session.execute(
                accessible_model_by_name(
                    agent.model.name,
                    user_id=principal.user.id,
                    group_ids=[principal.billing_group.id],
                )
            )
        )
        .unique()
        .scalars()
        .first()
    )
    if allowed is None:
        raise ModelNotFoundError(
            f"{name!r} runs on a model you do not have access to. Ask for access to "
            f"{agent.model.name!r}, or use an agent on a model you already have."
        )

    return ResolvedAgent(
        agent=agent, model=allowed, bases=await usable_bases(session, agent, principal)
    )


async def usable_bases(
    session: AsyncSession, agent: Agent, principal: Principal
) -> list[KnowledgeBase]:
    """The attached bases this caller may read, right now.

    Re-checked rather than trusted from the attachment, and the caller's access
    is what counts rather than the owner's. Two consequences worth stating:
    an agent shared with a colleague retrieves only from the bases *they* can
    reach, so sharing an agent is not a way to lend out a document; and an
    owner who loses access to a base stops retrieving from it on the next
    request, with nothing to invalidate.

    A base that is dropped is logged at debug and nowhere else. It is a normal
    consequence of a permission change, not a fault, and a warning per request
    would be noise in the log of a working deployment.
    """
    attached = [
        link.knowledge_base
        for link in agent.knowledge_bases
        if link.knowledge_base is not None and link.knowledge_base.is_active
    ]
    if not attached:
        return []

    group_ids = await sharing.effective_group_ids(session, principal.user.id)
    reachable = (
        await session.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id.in_([base.id for base in attached]),
                sharing.reachable(
                    kind=ResourceKind.KNOWLEDGE_BASE,
                    owner_column=KnowledgeBase.owner_user_id,
                    resource_id_column=KnowledgeBase.id,
                    user_id=principal.user.id,
                    group_ids=group_ids,
                    role=ShareRole.VIEWER,
                ),
            )
        )
    ).scalars()
    permitted = set(reachable)
    kept = [base for base in attached if base.id in permitted]
    if len(kept) != len(attached):
        logger.debug(
            "agent %s: %d of %d attached bases are not readable by user %s",
            agent.name,
            len(attached) - len(kept),
            len(attached),
            principal.user.id,
        )
    return kept


def last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    """What to retrieve on: the most recent user turn, flattened.

    The last user message rather than the whole conversation, because a
    retrieval query built from twenty turns is a query about nothing in
    particular — the earlier turns dilute it, and the embedding of a long
    conversation sits near the average of its topics rather than near the
    question just asked. Multi-turn query rewriting is the real answer and it
    needs a model call, which is a decision rather than an implementation
    detail.

    Handles the content-parts shape as well as a plain string: a multimodal
    client sends `[{"type": "text", ...}, {"type": "image_url", ...}]`, and
    reading `content` as a string there would retrieve on `"[{'type'..."`.
    """
    for message in reversed(list(messages)):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            joined = "\n".join(part for part in parts if part)
            if joined:
                return joined
    return ""


def format_context(passages: Sequence[tuple[str, str]]) -> str:
    """The retrieved passages as one system message.

    ``(title, text)`` pairs. Titles are included because a model asked to name
    its source cannot do so from the text alone, and because a reader checking
    an answer needs to know which document it came from.
    """
    blocks = [f"[{title}]\n{text}" for title, text in passages]
    return CONTEXT_PREAMBLE + "\n\n" + "\n\n---\n\n".join(blocks)


def apply(
    payload_messages: list[dict[str, Any]],
    resolved: ResolvedAgent,
    *,
    context: str | None = None,
) -> list[dict[str, Any]]:
    """The agent's prompt and retrieved context, in front of the conversation.

    Order is deliberate: the agent's instructions first, then the retrieved
    passages, then everything the caller sent — including their own system
    message, which is kept. A model reads its instructions before its material,
    and the caller's task description is the last thing it should see before
    the conversation itself.
    """
    prefix: list[dict[str, Any]] = []
    if resolved.agent.system_prompt.strip():
        prefix.append({"role": "system", "content": resolved.agent.system_prompt})
    if context:
        prefix.append({"role": "system", "content": context})
    return prefix + list(payload_messages)


def merge_tools(requested: Any, agent_tools: Sequence[Any]) -> list[dict[str, Any]] | None:
    # `Sequence[Any]` rather than `Sequence[dict]`, and the looser type is the
    # honest one: `agents.tools` is a JSON column, so its contents are whatever
    # was stored there rather than whatever the annotation claims. Typing it as
    # dicts made the isinstance guard below unreachable to mypy while leaving it
    # necessary at runtime, which is the wrong way round.
    """The agent's tools plus the request's, without duplicating a name.

    The request wins a collision: a caller who defines ``web_search`` with
    their own parameters means that one, and an agent's default silently
    replacing it would be the agent overriding an explicit instruction.

    Returns None when there is nothing to send, so a request that had no
    ``tools`` key does not acquire an empty array — some providers treat
    ``tools: []`` as "tool calling is enabled and there are none", which is a
    different statement from silence.
    """
    supplied = (
        [tool for tool in requested if isinstance(tool, dict)]
        if isinstance(requested, list)
        else []
    )

    def name_of(tool: dict[str, Any]) -> str | None:
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        # The date-versioned Anthropic shape, and anything else that names
        # itself at the top level.
        return str(tool["name"]) if tool.get("name") else None

    taken = {name for tool in supplied if (name := name_of(tool)) is not None}
    merged = list(supplied)
    for tool in agent_tools:
        if not isinstance(tool, dict):
            continue
        name = name_of(tool)
        if name is not None and name in taken:
            continue
        merged.append(tool)
    return merged or None


def apply_generation(payload: dict[str, Any], generation: dict[str, Any]) -> None:
    """Fill sampling defaults the request did not set, in place.

    Only where the caller said nothing. An agent supplies defaults, not
    overrides — a caller who asked for ``temperature: 0`` wants zero, and an
    agent quietly replacing it is the kind of surprise that makes people stop
    using agents.
    """
    for key, value in generation.items():
        if key not in payload or payload[key] is None:
            payload[key] = value
