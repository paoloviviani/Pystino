"""``GET /v1/models``.

Served from our own catalogue, filtered by the caller's group memberships — never
proxied from the upstream. Two reasons:

* per-group availability is the point: a caller must see exactly the models it may
  actually use, which the upstream knows nothing about;
* an upstream's model list is not trustworthy as a source of truth. A router
  whose configuration silently empties would otherwise make every model vanish
  from our clients' view too.

The union across *all* the user's groups is returned, not just the current billing
group, because a client discovering models should see everything it could reach
after switching group.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from gateway import agents as agent_logic
from gateway import sharing
from gateway.access import accessible_model_by_name, accessible_models
from gateway.deps import PrincipalDep, SessionDep
from gateway.models import Agent, ModelDef, ResourceKind
from gateway.schemas import ModelCard, ModelList

router = APIRouter(prefix="/v1", tags=["openai"])


@router.get("/models", response_model=ModelList)
async def list_models(principal: PrincipalDep, session: SessionDep) -> ModelList:
    # Group grants and personal grants, unioned in one place (ADR 0027). The
    # empty case is handled there too, so there is no early return to keep in
    # step with the predicate.
    stmt = accessible_models(
        user_id=principal.user.id, group_ids=principal.user.group_ids()
    ).order_by(ModelDef.name)
    models = (await session.execute(stmt)).scalars().all()

    # Agents are listed here too, as `agent:<name>` (ADR 0062). This is what
    # makes an agent usable from a client that has never heard of one: it
    # appears in the model picker, and sending it as `model` is all there is to
    # it. The card reports the *agent's* description and its underlying model's
    # capabilities, because those are what the caller's request is bound by.
    # `principal.user.group_ids()` rather than `sharing.effective_group_ids`,
    # which would be a second query for something already in hand: memberships
    # are eager-loaded to authenticate the request at all, which is the same
    # reason `accessible_models` above reads them this way. It cost a round trip
    # on every `/v1/models` call until `test_query_counts.py` said so.
    group_ids = principal.user.group_ids()
    agent_rows = await session.execute(
        select(Agent)
        .where(
            Agent.is_active.is_(True),
            sharing.reachable(
                kind=ResourceKind.AGENT,
                owner_column=Agent.owner_user_id,
                resource_id_column=Agent.id,
                user_id=principal.user.id,
                group_ids=group_ids,
            ),
        )
        .order_by(Agent.name)
    )
    reachable_models = {model.name for model in models}
    agent_cards = [
        ModelCard(
            id=agent_logic.wire_name(agent),
            created=int(agent.created_at.timestamp()),
            owned_by=agent.model.provider.name,
            context_window=agent.model.context_window,
            max_output_tokens=agent.model.max_output_tokens,
            display_name=agent.description or agent.name,
            kind=agent.model.kind.value,
            input_modalities=list(agent.model.input_modalities or []),
            output_modalities=list(agent.model.output_modalities or []),
            supported_features=list(agent.model.supported_features or []),
        )
        for agent in agent_rows.unique().scalars()
        # An agent on a model this caller cannot use would be a listing they
        # cannot act on: `agents.resolve` refuses it, so offering it here would
        # advertise a 404. Being shared an agent is not being granted its model.
        if agent.model.name in reachable_models
    ]

    return ModelList(
        data=agent_cards
        + [
            ModelCard(
                id=model.name,
                created=int(model.created_at.timestamp()),
                owned_by=model.provider.name,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                display_name=model.display_name,
                kind=model.kind.value,
                input_modalities=list(model.input_modalities or []),
                output_modalities=list(model.output_modalities or []),
                supported_features=list(model.supported_features or []),
            )
            for model in models
        ]
    )


@router.get("/models/{model_name:path}", response_model=ModelCard)
async def retrieve_model(
    model_name: str, principal: PrincipalDep, session: SessionDep
) -> ModelCard:
    from gateway.errors import ModelNotFoundError

    # An agent is addressed as a model, so it has to be retrievable as one:
    # a client that lists `agent:research` and then asks about it should not
    # get a 404 from the endpoint whose whole job is to describe what it just
    # listed.
    if agent_logic.is_agent_name(model_name):
        resolved = await agent_logic.resolve(session, model_name, principal=principal)
        if resolved is None:  # pragma: no cover - is_agent_name just said it is one
            raise ModelNotFoundError(f"The model {model_name!r} does not exist.")
        return ModelCard(
            id=agent_logic.wire_name(resolved.agent),
            created=int(resolved.agent.created_at.timestamp()),
            owned_by=resolved.model.provider.name,
            context_window=resolved.model.context_window,
            max_output_tokens=resolved.model.max_output_tokens,
            display_name=resolved.agent.description or resolved.agent.name,
            kind=resolved.model.kind.value,
            input_modalities=list(resolved.model.input_modalities or []),
            output_modalities=list(resolved.model.output_modalities or []),
            supported_features=list(resolved.model.supported_features or []),
        )

    stmt = accessible_model_by_name(
        model_name, user_id=principal.user.id, group_ids=principal.user.group_ids()
    )
    model = (await session.execute(stmt)).scalars().first()
    if model is None:
        raise ModelNotFoundError(
            f"The model {model_name!r} does not exist or you do not have access to it."
        )
    return ModelCard(
        id=model.name,
        created=int(model.created_at.timestamp()),
        owned_by=model.provider.name,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        display_name=model.display_name,
        kind=model.kind.value,
        input_modalities=list(model.input_modalities or []),
        output_modalities=list(model.output_modalities or []),
        supported_features=list(model.supported_features or []),
    )
