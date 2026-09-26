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

**The list is public; a card's detail is not** (ADR 0081). An unauthenticated
``GET /v1/models`` answers with the full, unfiltered catalogue rather than 401ing
— brochure-level fields only (ids, names, context/token limits, modalities,
features; no prices, no grants, no usage), the same shape an authenticated caller
already sees per model. Grant filtering only ever *narrows* what a real caller may
reach; skipping it for a caller who does not exist is not the same operation as
running it against one with no grants, which is why this is a branch here rather
than a call to `accessible_models` with an empty principal. `GET
/v1/models/{name}` stays authenticated — resolving one named model is what a
caller does once it already has a credential to spend, and that is exactly the
step the exemption does not cover.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import or_

from gateway.access import accessible_model_by_name, accessible_models, all_active_models
from gateway.deps import OptionalPrincipalDep, PrincipalDep, SessionDep
from gateway.errors import BadRequestError
from gateway.models import ModelDef, ModelKind, Provider, ProviderKind
from gateway.schemas import ModelCard, ModelList

#: The `?include=` vocabulary. Search is the odd one out — its tiers answer
#: /v1/search and stay out of the default list for the reason in the docstring
#: above.
_KINDS: dict[str, ModelKind] = {kind.value: kind for kind in ModelKind}

#: Models behind this kind of provider are plumbing, not catalogue entries:
#: the deployment's own extractor reads documents on `/v1/ocr`, and a caller
#: picking "markitdown" from a model list is picking plumbing as if it were a
#: choice. The listing is the only place they are hidden — a caller whose
#: grants reach one may still ask for it by name (retrieval and resolution are
#: unchanged), which is the same line the search tiers walk.
_NOT_CALLER_FACING = ProviderKind.INTERNAL

router = APIRouter(prefix="/v1", tags=["openai"])


@router.get("/models", response_model=ModelList)
async def list_models(
    principal: OptionalPrincipalDep,
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
    explicit_ocr = False
    if include:
        kinds = {kind for part in include.split(",") if (kind := _KINDS.get(part.strip()))}
        if not kinds:
            raise BadRequestError(
                f"Unknown kinds in 'include'. Known: {', '.join(sorted(_KINDS))}."
            )
        # An internal-provider model — this deployment's own extractor — is
        # plumbing, not a catalogue entry a caller picks (`_NOT_CALLER_FACING`
        # above), and stays hidden even though `ModelKind.OCR` is one of the
        # kinds the unqualified default already includes below. It appears
        # only when `ocr` was named *explicitly* here, which is the opt-in a
        # caller such as Cerea's Knowledge screen uses to ask "what can read a
        # document here, including this server's own reader" — never as a side
        # effect of the default kind set happening to contain OCR. A non-OCR
        # internal model (none exist today, and the search tiers are a
        # separate kind entirely) stays hidden regardless: the opt-in is per
        # kind, not per provider.
        explicit_ocr = ModelKind.OCR in kinds
    else:
        kinds = set(ModelKind) - {ModelKind.SEARCH}

    # Group grants and personal grants, unioned in one place (ADR 0027). The
    # empty case is handled there too, so there is no early return to keep in
    # step with the predicate. No principal at all (ADR 0081) skips grant
    # filtering entirely rather than running it against nothing — the full
    # catalogue, not the subset a groupless caller would see.
    base = (
        accessible_models(user_id=principal.user.id, group_ids=principal.user.group_ids())
        if principal is not None
        else all_active_models()
    )
    visible = Provider.kind != _NOT_CALLER_FACING
    if explicit_ocr:
        visible = or_(visible, ModelDef.kind == ModelKind.OCR)
    stmt = base.where(ModelDef.kind.in_(kinds), visible).order_by(ModelDef.name)
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
                local=model.provider.kind == _NOT_CALLER_FACING,
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
