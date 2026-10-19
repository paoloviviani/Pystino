"""``GET /v1/models``.

Served from our own catalogue, filtered by the caller's group memberships — never
proxied from the upstream. Two reasons:

* per-group availability is the point: a caller must see exactly the models it may
  actually use, which the upstream knows nothing about;
* an upstream's model list is not trustworthy as a source of truth. A router
  whose configuration silently empties would otherwise make every model vanish
  from our clients' view too.

The list follows the *billing group* — the same group a request is admitted
against, `principal.billing_group`, which is where ``x-bill-to`` lands in
(`gateway.deps`). It used to be the union across **all** the person's groups,
because a client discovering models might expect to see everything it could
reach after switching group — and that is exactly the mismatch the owner
reported: a model granted to group B was listed to someone billing group A,
and every request on it answered 404, because `resolve_model` (the request
path) scopes by the billing group only. The decision now is that the list
shows exactly what a request would accept, nothing more: discovery uses the
same scope as admission. A client that wants the other group's view sends
``x-bill-to`` (as Cerea's model list does, `src/lib/server/billTo.ts`).
Personal grants and public models span billing groups, so they stay listed
from whichever group is paying.

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

from typing import Any

from fastapi import APIRouter
from sqlalchemy import Select, or_
from sqlalchemy.orm import aliased

from gateway.access import (
    accessible_model_by_name,
    accessible_models,
    alias_unavailable_reason,
    all_active_models,
)
from gateway.deps import OptionalPrincipalDep, PrincipalDep, SessionDep
from gateway.errors import BadRequestError
from gateway.models import ModelDef, ModelKind, ModelRouteTarget, Provider, ProviderKind
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

    # Billing-group grants plus personal grants, unioned in one place (ADR
    # 0027) — and scoped to the billing group, because that is the group a
    # request would be admitted against (`resolve_model`): the list answering
    # more than that is the mismatch this route once shipped (see the
    # docstring). The empty case is handled there too, so there is no early
    # return to keep in step with the predicate. No principal at all (ADR
    # 0081) skips grant filtering entirely rather than running it against
    # nothing — the full catalogue, not the subset a groupless caller would
    # see.
    base = (
        accessible_models(user_id=principal.user.id, group_ids=[principal.billing_group.id])
        if principal is not None
        else all_active_models()
    )
    visible = Provider.kind != _NOT_CALLER_FACING
    if explicit_ocr:
        visible = or_(visible, ModelDef.kind == ModelKind.OCR)
    # An alias has no provider, so `Provider.kind != _NOT_CALLER_FACING` reads
    # NULL for it and the comparison is not true — every alias would vanish
    # from the listing while its target was right there. Internal-ness is the
    # *target's* property (see `_card`), and this listing already hides internal
    # models by default, so the arm here is only about not dropping the row
    # before `_card` gets to decide.
    visible = or_(visible, ModelDef.is_alias.is_(True))

    stmt = _with_alias_target(base.where(ModelDef.kind.in_(kinds), visible)).order_by(
        ModelDef.name, ModelRouteTarget.position
    )
    rows = (await session.execute(stmt)).all()

    cards = []
    for model, target, target_provider in rows:
        card = _card(model, target, target_provider)
        if card is not None:
            cards.append(card)

    return ModelList(data=cards)


#: The `models` row an alias points at, and its provider, as two more things to
#: select from the **same** statement.
_Target = aliased(ModelDef)
_TargetProvider = aliased(Provider)


def _with_alias_target(statement: Select[Any]) -> Select[Any]:
    """Attach an alias's target and the target's provider to ``statement``.

    Two left joins and two extra selected columns, rather than a
    `selectinload`. The reason is `test_query_counts.py`: `/v1/models` is pinned
    at four selects, and `selectinload` on a collection issues its round trip
    whether or not any row needs it — a deployment with no aliases at all would
    pay for the feature. Joining keeps the count at four with any number of
    aliases, and one alias's target is one row rather than a query, so the cost
    does not grow with the size of the catalogue either.

    Left joins throughout, so a served model — which has no route target — comes
    back with two nulls rather than being filtered away, and an alias whose
    target was deleted (the cascade removed the route row) comes back as one too
    rather than vanishing before `_card` can say why.
    """
    return (
        statement.outerjoin(ModelRouteTarget, ModelRouteTarget.alias_id == ModelDef.id)
        .outerjoin(_Target, _Target.id == ModelRouteTarget.target_model_id)
        .outerjoin(_TargetProvider, _TargetProvider.id == _Target.provider_id)
        .add_columns(_Target, _TargetProvider)
    )


def _card(
    model: ModelDef, target: ModelDef | None, target_provider: Provider | None
) -> ModelCard | None:
    """The card for one catalogue row, or None if it must not be listed.

    An alias's card is **assembled from two rows and names only one of them**
    (ADR 0101, decision 6): the name, `display_name` and `description` are the
    alias's own — they are what an administrator wrote for this name and what a
    caller must see — while every capability is the target's, because the target
    is what will actually run the request and a card that under-promises is a
    request that 400s. The target's own name, id and provider are absent from the
    card entirely: retargeting has to be invisible, and the target may be a
    model this caller cannot see.

    ``owned_by`` is the only field where the two would have collided, and it is
    the reason it reads "gateway" rather than the target's provider name. The
    one thing the target's provider is allowed to decide is ``local``: it is a
    boolean about where bytes go, not an identity, and answering ``false`` for an
    alias onto this deployment's own extractor would tell a client to send a
    document off-box that never had to leave.
    """
    if not model.is_alias:
        return ModelCard(
            id=model.name,
            created=int(model.created_at.timestamp()),
            owned_by=model.provider.name if model.provider is not None else "gateway",
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            max_input_tokens=model.max_input_tokens,
            display_name=model.display_name,
            description=model.description,
            kind=model.kind.value,
            input_modalities=list(model.input_modalities or []),
            output_modalities=list(model.output_modalities or []),
            supported_features=list(model.supported_features or []),
            local=model.provider.kind == _NOT_CALLER_FACING
            if model.provider is not None
            else False,
        )

    # Unavailable aliases are left out (ADR 0101, decision 5) — a card is a
    # promise that the name works, and `retrieve_model` 404s for the same rows.
    if target is None or alias_unavailable_reason(model, target) is not None:
        return None

    return ModelCard(
        id=model.name,
        created=int(model.created_at.timestamp()),
        # Never the target's provider name: see the docstring.
        owned_by="gateway",
        context_window=target.context_window,
        max_output_tokens=target.max_output_tokens,
        max_input_tokens=target.max_input_tokens,
        display_name=model.display_name,
        description=model.description,
        kind=target.kind.value,
        input_modalities=list(target.input_modalities or []),
        output_modalities=list(target.output_modalities or []),
        supported_features=list(target.supported_features or []),
        local=target_provider.kind == _NOT_CALLER_FACING if target_provider is not None else False,
    )


@router.get("/models/{model_name:path}", response_model=ModelCard)
async def retrieve_model(
    model_name: str, principal: PrincipalDep, session: SessionDep
) -> ModelCard:
    from gateway.errors import ModelNotFoundError

    # The same scope the request path admits on (`resolve_model`): billing
    # group, personal grants, public. Retrieval disagreeing with the listing
    # — here, admitting a name the list would not show — was the other half of
    # the mismatch the listing once had.
    stmt = accessible_model_by_name(
        model_name, user_id=principal.user.id, group_ids=[principal.billing_group.id]
    )
    row = (await session.execute(_with_alias_target(stmt))).first()
    if row is None:
        raise ModelNotFoundError(
            f"The model {model_name!r} does not exist or you do not have access to it."
        )
    model, target, target_provider = row
    # One row can be missing from the listing for two reasons that are the same
    # answer to a caller: the name is not theirs, or the alias behind it has
    # nothing to serve. Both are a 404 here — this route has always answered
    # "not available to you" for an inaccessible model, and an alias whose
    # target is deleted is not available.
    card = _card(model, target, target_provider)
    if card is None:
        raise ModelNotFoundError(
            f"The model {model_name!r} does not exist or you do not have access to it."
        )
    return card
