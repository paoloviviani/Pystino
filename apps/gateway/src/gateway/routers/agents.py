"""``/v1/agents`` — authoring the things ``/v1/models`` then offers.

Two surfaces for one concept, and the split is the point. **Using** an agent
happens through ``/v1/chat/completions`` with ``model: "agent:<name>"``, so any
OpenAI-compatible client can do it with no code that knows agents exist.
**Authoring** one needs a shape OpenAI has no equivalent for — a system prompt,
default tools, attached knowledge bases — and that is this file.

Ownership and sharing are the knowledge base's, exactly: `resource_shares` with
kind ``agent``, the same predicate in `sharing.py`, and the same rule that only
the owner may delete or re-share. A second sharing mechanism is how a
deployment ends up with somebody who can reach a resource through one door and
not the other.

**Attaching a base you can read does not lend it out.** Retrieval re-checks the
*caller's* access to every attached base on every request (`agents.usable_bases`),
so an agent shared with a colleague retrieves only from the bases they can
reach themselves. Without that, sharing an agent would be a way to publish a
document without sharing the document.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from gateway import agents as agent_logic
from gateway import sharing
from gateway.access import accessible_model_by_name
from gateway.deps import Principal, PrincipalDep, SessionDep
from gateway.errors import BadRequestError, NotFoundError, PermissionError_
from gateway.models import (
    Agent,
    AgentKnowledgeBase,
    Group,
    KnowledgeBase,
    ModelKind,
    ResourceKind,
    SharePrincipal,
    ShareRole,
    User,
)
from gateway.routers.files import KnowledgeDep

router = APIRouter(prefix="/v1", tags=["pystino"])

KIND = ResourceKind.AGENT


class AgentCreate(BaseModel):
    #: Without the `agent:` prefix. Accepting it would mean two spellings of
    #: one name and a `agent:agent:research` the first time somebody pastes
    #: what they saw in `/v1/models`.
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    description: str = Field(default="", max_length=500)
    #: The served model this agent runs on, by name.
    model: str
    system_prompt: str = Field(default="", max_length=32_000)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    generation: dict[str, Any] = Field(default_factory=dict)
    knowledge_base_ids: list[uuid.UUID] = Field(default_factory=list)
    retrieval_limit: int = Field(default=6, ge=1, le=50)
    retrieval_min_score: float = Field(default=0.0, ge=-1.0, le=1.0)


class AgentUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    model: str | None = None
    system_prompt: str | None = Field(default=None, max_length=32_000)
    tools: list[dict[str, Any]] | None = None
    generation: dict[str, Any] | None = None
    knowledge_base_ids: list[uuid.UUID] | None = None
    retrieval_limit: int | None = Field(default=None, ge=1, le=50)
    retrieval_min_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    is_active: bool | None = None


class AgentObject(BaseModel):
    id: uuid.UUID
    object: Literal["agent"] = "agent"
    #: The wire name, with the prefix — what a caller puts in `model`. The
    #: bare name is in `name`, so a client editing an agent does not have to
    #: strip anything.
    model_name: str
    name: str
    created_at: int
    description: str
    model: str
    system_prompt: str
    tools: list[dict[str, Any]]
    generation: dict[str, Any]
    knowledge_base_ids: list[uuid.UUID]
    retrieval_limit: int
    retrieval_min_score: float
    is_active: bool
    owned: bool
    role: str


class AgentList(BaseModel):
    object: Literal["list"] = "list"
    data: list[AgentObject]


def _object(agent: Agent, *, principal: Principal, role: str) -> AgentObject:
    return AgentObject(
        id=agent.id,
        model_name=agent_logic.wire_name(agent),
        name=agent.name,
        created_at=int(agent.created_at.timestamp()),
        description=agent.description,
        model=agent.model.name,
        system_prompt=agent.system_prompt,
        tools=list(agent.tools or []),
        generation=dict(agent.generation or {}),
        knowledge_base_ids=[link.knowledge_base_id for link in agent.knowledge_bases],
        retrieval_limit=agent.retrieval_limit,
        retrieval_min_score=agent.retrieval_min_score,
        is_active=agent.is_active,
        owned=agent.owner_user_id == principal.user.id,
        role=role,
    )


async def _load(session: SessionDep, agent_id: uuid.UUID) -> Agent:
    result = await session.execute(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.knowledge_bases))
    )
    agent = result.unique().scalars().first()
    if agent is None:
        raise NotFoundError(f"No such agent: {agent_id}")
    return agent


async def _reachable(
    session: SessionDep,
    principal: Principal,
    agent_id: uuid.UUID,
    *,
    role: ShareRole = ShareRole.VIEWER,
) -> Agent:
    """One agent this caller may reach, in this role.

    Same two answers as a knowledge base, for the same reasons: invisible is a
    404 because whose agents exist is not this caller's business, and visible
    but read-only is a 403 because that is a fact about their role rather than
    about the resource.
    """
    agent = await _load(session, agent_id)
    if not await sharing.may_reach(
        session,
        kind=KIND,
        resource_id=agent.id,
        owner_user_id=agent.owner_user_id,
        user_id=principal.user.id,
    ):
        raise NotFoundError(f"No such agent: {agent_id}")
    if role is not ShareRole.VIEWER and not await sharing.may_reach(
        session,
        kind=KIND,
        resource_id=agent.id,
        owner_user_id=agent.owner_user_id,
        user_id=principal.user.id,
        role=role,
    ):
        raise PermissionError_("You have read-only access to this agent.")
    return agent


async def _resolve_model(session: SessionDep, principal: Principal, name: str) -> uuid.UUID:
    """The chat model this caller may use, by name.

    Checked against `access.py` rather than merely looked up: an agent is not a
    way to reach a model somebody has not been granted, and building one on a
    model you cannot use would produce an agent that 404s for its own author.
    """
    model = (
        (
            await session.execute(
                accessible_model_by_name(
                    name, user_id=principal.user.id, group_ids=[principal.billing_group.id]
                )
            )
        )
        .unique()
        .scalars()
        .first()
    )
    if model is None:
        raise BadRequestError(
            f"No model named {name!r} that you can use.", code="unknown_model"
        )
    if model.kind is not ModelKind.CHAT:
        raise BadRequestError(
            f"{name!r} is a {model.kind.value} model. An agent runs on a chat model.",
            code="wrong_model_kind",
        )
    return model.id


async def _attach(
    session: SessionDep, principal: Principal, agent: Agent, base_ids: list[uuid.UUID]
) -> None:
    """Replace the attached bases, refusing any this caller cannot read.

    Refused rather than silently dropped: an author who attaches a base they
    cannot see has made a mistake, and an agent that quietly retrieves from
    fewer bases than its screen shows is the kind of thing nobody debugs.
    """
    wanted = list(dict.fromkeys(base_ids))
    if wanted:
        group_ids = await sharing.effective_group_ids(session, principal.user.id)
        readable = set(
            (
                await session.execute(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.id.in_(wanted),
                        KnowledgeBase.is_active.is_(True),
                        sharing.reachable(
                            kind=ResourceKind.KNOWLEDGE_BASE,
                            owner_column=KnowledgeBase.owner_user_id,
                            resource_id_column=KnowledgeBase.id,
                            user_id=principal.user.id,
                            group_ids=group_ids,
                        ),
                    )
                )
            ).scalars()
        )
        missing = [str(base_id) for base_id in wanted if base_id not in readable]
        if missing:
            raise BadRequestError(
                "These knowledge bases do not exist or you cannot read them: "
                + ", ".join(missing),
                code="unknown_knowledge_base",
            )

    await session.execute(
        delete(AgentKnowledgeBase).where(AgentKnowledgeBase.agent_id == agent.id)
    )
    for base_id in wanted:
        session.add(AgentKnowledgeBase(agent_id=agent.id, knowledge_base_id=base_id))


@router.post("/agents", response_model=AgentObject)
async def create_agent(
    body: AgentCreate,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> AgentObject:
    existing = (
        await session.execute(select(Agent.id).where(Agent.name == body.name))
    ).scalars().first()
    if existing is not None:
        # A 409 rather than a silent rename: the name is how the agent is
        # addressed, so the caller has to know which one they got.
        raise BadRequestError(
            f"An agent named {body.name!r} already exists.", code="duplicate_agent"
        )

    model_id = await _resolve_model(session, principal, body.model)
    agent = Agent(
        name=body.name,
        description=body.description,
        owner_user_id=principal.user.id,
        billing_group_id=principal.billing_group.id,
        model_id=model_id,
        system_prompt=body.system_prompt,
        tools=body.tools,
        generation=body.generation,
        retrieval_limit=body.retrieval_limit,
        retrieval_min_score=body.retrieval_min_score,
    )
    session.add(agent)
    await session.flush()
    await _attach(session, principal, agent, body.knowledge_base_ids)
    await session.commit()
    return _object(await _load(session, agent.id), principal=principal, role="owner")


@router.get("/agents", response_model=AgentList)
async def list_agents(
    session: SessionDep, principal: PrincipalDep, knowledge: KnowledgeDep
) -> AgentList:
    group_ids = await sharing.effective_group_ids(session, principal.user.id)
    rows = await session.execute(
        select(Agent)
        .where(
            Agent.is_active.is_(True),
            sharing.reachable(
                kind=KIND,
                owner_column=Agent.owner_user_id,
                resource_id_column=Agent.id,
                user_id=principal.user.id,
                group_ids=group_ids,
            ),
        )
        .options(selectinload(Agent.knowledge_bases))
        .order_by(Agent.name)
    )
    listed = rows.unique().scalars().all()
    return AgentList(
        data=[
            _object(
                agent,
                principal=principal,
                role="owner" if agent.owner_user_id == principal.user.id else "viewer",
            )
            for agent in listed
        ]
    )


@router.get("/agents/{agent_id}", response_model=AgentObject)
async def retrieve_agent(
    agent_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> AgentObject:
    agent = await _reachable(session, principal, agent_id)
    return _object(
        agent,
        principal=principal,
        role="owner" if agent.owner_user_id == principal.user.id else "viewer",
    )


@router.post("/agents/{agent_id}", response_model=AgentObject)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> AgentObject:
    agent = await _reachable(session, principal, agent_id, role=ShareRole.EDITOR)
    if body.model is not None:
        agent.model_id = await _resolve_model(session, principal, body.model)
    if body.description is not None:
        agent.description = body.description
    if body.system_prompt is not None:
        agent.system_prompt = body.system_prompt
    if body.tools is not None:
        agent.tools = body.tools
    if body.generation is not None:
        agent.generation = body.generation
    if body.retrieval_limit is not None:
        agent.retrieval_limit = body.retrieval_limit
    if body.retrieval_min_score is not None:
        agent.retrieval_min_score = body.retrieval_min_score
    if body.is_active is not None:
        agent.is_active = body.is_active
    if body.knowledge_base_ids is not None:
        await _attach(session, principal, agent, body.knowledge_base_ids)
    await session.commit()
    return _object(
        await _load(session, agent.id),
        principal=principal,
        role="owner" if agent.owner_user_id == principal.user.id else "editor",
    )


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> dict[str, object]:
    """Only the owner, and it takes the grants with it.

    Not an editor's right, for the reason it is not on a knowledge base:
    reading "you may edit this" as "you may destroy it" is an inference an ACL
    should never make.
    """
    agent = await _load(session, agent_id)
    if agent.owner_user_id != principal.user.id:
        if await sharing.may_reach(
            session,
            kind=KIND,
            resource_id=agent.id,
            owner_user_id=agent.owner_user_id,
            user_id=principal.user.id,
        ):
            raise PermissionError_("Only the owner can delete an agent.")
        raise NotFoundError(f"No such agent: {agent_id}")
    await sharing.delete_shares(session, kind=KIND, resource_id=agent.id)
    await session.execute(delete(Agent).where(Agent.id == agent.id))
    await session.commit()
    return {"id": str(agent_id), "object": "agent.deleted", "deleted": True}


class AgentShareIn(BaseModel):
    principal_kind: SharePrincipal
    principal_id: uuid.UUID
    role: ShareRole = ShareRole.VIEWER


@router.post("/agents/{agent_id}/shares")
async def share_agent(
    agent_id: uuid.UUID,
    body: AgentShareIn,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> dict[str, object]:
    """Share it. Owner only.

    Sharing an agent does **not** share its knowledge bases: retrieval
    re-checks the caller's own access on every request, so a recipient sees
    passages only from bases they could already read. That is what stops an
    agent being a way to publish a document without sharing the document.
    """
    agent = await _load(session, agent_id)
    if agent.owner_user_id != principal.user.id:
        raise NotFoundError(f"No such agent: {agent_id}")

    if body.principal_kind is SharePrincipal.USER:
        target = await session.get(User, body.principal_id)
        if target is None or not target.is_active:
            raise BadRequestError("No such user.", code="unknown_principal")
    else:
        group = await session.get(Group, body.principal_id)
        if group is None or not group.is_active:
            raise BadRequestError("No such group.", code="unknown_principal")

    await sharing.grant(
        session,
        kind=KIND,
        resource_id=agent.id,
        principal_kind=body.principal_kind,
        principal_id=body.principal_id,
        role=body.role,
        granted_by=principal.user.id,
    )
    await session.commit()
    return {"object": "agent.share", "shared": True}


@router.delete("/agents/{agent_id}/shares/{principal_kind}/{principal_id}")
async def unshare_agent(
    agent_id: uuid.UUID,
    principal_kind: SharePrincipal,
    principal_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    knowledge: KnowledgeDep,
) -> dict[str, object]:
    agent = await _load(session, agent_id)
    if agent.owner_user_id != principal.user.id:
        raise NotFoundError(f"No such agent: {agent_id}")
    removed = await sharing.revoke(
        session,
        kind=KIND,
        resource_id=agent.id,
        principal_kind=principal_kind,
        principal_id=principal_id,
    )
    await session.commit()
    return {"object": "agent.share.deleted", "deleted": removed}
