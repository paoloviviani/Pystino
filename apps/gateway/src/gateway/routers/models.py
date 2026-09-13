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
from gateway.errors import BadRequestError
from gateway.models import ModelDef, ModelKind
from gateway.schemas import ModelCard, ModelList

#: The `?include=` vocabulary. Search is the odd one out — its tiers answer
#: /v1/search and stay out of the default list for the reason in the docstring
#: above.
_KINDS: dict[str, ModelKind] = {kind.value: kind for kind in ModelKind}

router = APIRouter(prefix="/v1", tags=["openai"])


@router.get("/models", response_model=ModelList)
async def list_models(
    principal: PrincipalDep,
    session: SessionDep,
    include: str | None = None,
) -> ModelList:
    # Search backends are absent unless asked for by name (ADR 0071). They
    # answer /v1/search, not /v1/chat/completions, and an OpenAI-compatible
    # client reading this list would present a tier as a chat model — the one
    # confusion this surface must not create. The kinds an ordinary client
    # expects ride along; `?include=search` is the opt-in for the rest, and
    # the caller's grants still bound whatever is shown.
    kinds: set[ModelKind] | None = None
    if include:
        kinds = {kind for part in include.split(",") if (kind := _KINDS.get(part.strip()))}
        if not kinds:
            raise BadRequestError(
                f"Unknown kinds in 'include'. Known: {', '.join(sorted(_KINDS))}."
            )
    else:
        kinds = set(ModelKind) - {ModelKind.SEARCH}

    # Group grants and personal grants, unioned in one place (ADR 0027). The
    # empty case is handled there too, so there is no early return to keep in
    # step with the predicate.
    stmt = (
        accessible_models(user_id=principal.user.id, group_ids=principal.user.group_ids())
        .where(ModelDef.kind.in_(kinds))
        .order_by(ModelDef.name)
    )
    models = (await session.execute(stmt)).scalars().all()

    return ModelList(
        data=[
            ModelCard(
                id=model.name,
                created=int(model.created_at.timestamp()),
                owned_by=model.provider.name,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                max_input_tokens=model.max_input_tokens,
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
        max_input_tokens=model.max_input_tokens,
        display_name=model.display_name,
        kind=model.kind.value,
        input_modalities=list(model.input_modalities or []),
        output_modalities=list(model.output_modalities or []),
        supported_features=list(model.supported_features or []),
    )
