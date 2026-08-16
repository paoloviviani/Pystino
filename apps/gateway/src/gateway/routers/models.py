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

from gateway.access import accessible_model_by_name, accessible_models
from gateway.deps import PrincipalDep, SessionDep
from gateway.models import ModelDef
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

    return ModelList(
        data=[
            ModelCard(
                id=model.name,
                created=int(model.created_at.timestamp()),
                owned_by=model.provider.name,
                context_window=model.context_window,
                display_name=model.display_name,
                kind=model.kind.value,
            )
            for model in models
        ]
    )


@router.get("/models/{model_name:path}", response_model=ModelCard)
async def retrieve_model(
    model_name: str, principal: PrincipalDep, session: SessionDep
) -> ModelCard:
    from gateway.errors import ModelNotFoundError

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
        display_name=model.display_name,
        kind=model.kind.value,
    )
