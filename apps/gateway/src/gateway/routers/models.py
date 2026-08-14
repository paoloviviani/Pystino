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

from gateway.deps import PrincipalDep, SessionDep
from gateway.models import GroupModelAccess, ModelDef
from gateway.schemas import ModelCard, ModelList

router = APIRouter(prefix="/v1", tags=["openai"])


@router.get("/models", response_model=ModelList)
async def list_models(principal: PrincipalDep, session: SessionDep) -> ModelList:
    group_ids = principal.user.group_ids()
    if not group_ids:
        return ModelList(data=[])

    stmt = (
        select(ModelDef)
        .join(GroupModelAccess, GroupModelAccess.model_id == ModelDef.id)
        .where(
            ModelDef.is_active.is_(True),
            GroupModelAccess.group_id.in_(group_ids),
        )
        .distinct()
        .order_by(ModelDef.name)
    )
    models = (await session.execute(stmt)).scalars().all()

    return ModelList(
        data=[
            ModelCard(
                id=model.name,
                created=int(model.created_at.timestamp()),
                owned_by=model.provider,
                context_window=model.context_window,
                display_name=model.display_name,
            )
            for model in models
        ]
    )


@router.get("/models/{model_name:path}", response_model=ModelCard)
async def retrieve_model(
    model_name: str, principal: PrincipalDep, session: SessionDep
) -> ModelCard:
    from gateway.errors import ModelNotFoundError

    group_ids = principal.user.group_ids()
    stmt = (
        select(ModelDef)
        .join(GroupModelAccess, GroupModelAccess.model_id == ModelDef.id)
        .where(
            ModelDef.name == model_name,
            ModelDef.is_active.is_(True),
            GroupModelAccess.group_id.in_(group_ids or [None]),
        )
    )
    model = (await session.execute(stmt)).scalars().first()
    if model is None:
        raise ModelNotFoundError(
            f"The model {model_name!r} does not exist or you do not have access to it."
        )
    return ModelCard(
        id=model.name,
        created=int(model.created_at.timestamp()),
        owned_by=model.provider,
        context_window=model.context_window,
        display_name=model.display_name,
    )
