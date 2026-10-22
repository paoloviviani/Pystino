"""``POST /v1/decisions`` and ``POST /v1/systemone`` (ADR 0100).

Decision models do not write text. They take a text and typed questions and
answer with calibrated probabilities, which makes routing, triage, moderation
and classification far cheaper than asking a chat model for a verdict and
parsing its prose. This route puts them behind the same keys, groups, quotas
and reports as everything else.

The pipeline is the embeddings one (resolve, check access, redact, reserve,
call, record, settle), and the differences are the shape of the operation:

**The request and the response pass through in the format they arrived in.**
Two formats exist and the caller picks: the ``input`` form (``input`` a string
or messages, ``questions`` an array) and the System One form (``state``,
``questions`` an object keyed by the caller's ids). ``/v1/decisions`` takes
both and answers in kind; ``/v1/systemone`` takes only the second. The gateway
reads neither the questions nor the answers: it validates which format this
is, and that is all, because it is a beta schema and a gateway that
re-validated it would refuse valid requests each time the provider added a
question type.

**Routing belongs to the deployment.** The body fields that choose providers,
regions, quantisations or fallbacks (the provider plugin names them) are
refused with a 400 naming the field. A caller who could send ``eu_native:
false`` could pick a provider the deployment excluded; as for chat, those are
the provider row's settings. ``preference`` (speed, cost, balanced) is the
caller's and passes through.

**Redaction applies to the text.** ``input`` and ``state`` go to an upstream,
so they go through the deployment's redaction policy exactly as a chat message
does, every string leaf of a ``state`` object included (keys are structure and
are not redacted). There is nothing to restore on the way back: answers carry
no free text but a refusal's reason, which passes through as written.

**The reservation is the prompt alone.** Output tokens are zero in everything
measured (the answer is numbers), so only the input is reserved, counted over
the redacted text and the questions, since the provider tokenises both.

**Never streamed.** The API has none.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, Literal

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.accounting import TokenCounts
from gateway.deps import (
    EstimatorDep,
    PrincipalDep,
    ProvidersDep,
    QuotaDep,
    RedactorDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError
from gateway.models import ApiSurface, ModelDef, Provider
from gateway.plugins import registry
from gateway.plugins.base import DecisionSupport
from gateway.routers import _metered
from gateway.routing import affinity_key
from gateway.schemas import DecisionsRequest
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.DECISIONS

Route = Literal["decisions", "systemone"]

_STATE_ONLY = "/v1/systemone takes the System One format: 'state' and a 'questions' object."


def decision_support(provider: Provider | None) -> DecisionSupport | None:
    """What a provider's plugin can do for decisions, or None.

    None for a provider with no decision support and for one whose plugin is not
    installed: either way this provider cannot serve the model, and the answer
    is the same 400.
    """
    if provider is None:
        return None
    try:
        plugin = registry.resolve(provider.plugin)
    except registry.UnknownPluginError:
        return None
    support = getattr(plugin, "decisions", None)
    return support if isinstance(support, DecisionSupport) else None


def require_decision_support(provider: Provider | None, model_name: str) -> DecisionSupport:
    support = decision_support(provider)
    if support is None:
        plugin = provider.plugin if provider else None
        raise BadRequestError(
            f"The provider behind model {model_name!r} (type {plugin or 'generic'!r}) has no "
            "decision-model support. Serve a decision model through a provider type that does.",
            code="provider_no_decision_support",
        )
    return support


def _strings(value: Any) -> Iterator[str]:
    """Every non-empty string leaf of a JSON value, in a stable order.

    Dict keys are not yielded: they are the caller's structure, and what a
    redaction policy exists to catch is values that carry personal data.
    """
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _rebuild(value: Any, replacements: Iterator[str]) -> Any:
    """*value* with each string leaf `_strings` yielded replaced, in the same order."""
    if isinstance(value, str):
        return next(replacements) if value else value
    if isinstance(value, dict):
        return {key: _rebuild(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebuild(item, replacements) for item in value]
    return value


def _validate(body: DecisionsRequest, route: Route) -> Literal["input", "state"]:
    fmt = body.format()
    if fmt is None:
        raise BadRequestError(
            "Send either 'input' with a 'questions' array, or 'state' with a 'questions' "
            "object, not both and not neither.",
            code="invalid_decisions_request",
        )
    if route == "systemone" and fmt != "state":
        raise BadRequestError(_STATE_ONLY, code="invalid_decisions_request")
    questions = body.questions
    if fmt == "input" and not (isinstance(questions, list) and questions):
        raise BadRequestError(
            "'questions' must be a non-empty array when 'input' is used.",
            code="invalid_decisions_request",
        )
    if fmt == "state" and not (isinstance(questions, dict) and questions):
        raise BadRequestError(
            "'questions' must be a non-empty object keyed by question id when 'state' is used.",
            code="invalid_decisions_request",
        )
    return fmt


def _refuse_routing_fields(
    payload: dict[str, Any], support: DecisionSupport, model: ModelDef
) -> None:
    """400 on a body field that is the provider's routing setting; drop its nulls.

    The error names the field and the reason, and nothing about the provider's
    own policy beyond that it exists: the caller cannot act on more.
    """
    for name in support.routing_fields:
        if name not in payload:
            continue
        if payload[name] is None:
            del payload[name]
            continue
        raise BadRequestError(
            f"'{name}' is a routing setting, and routing is configured on the provider "
            f"behind model {model.name!r} by this deployment, not chosen per request. "
            "Remove it from the request.",
            code="routing_field_refused",
        )


async def _decide(
    route: Route,
    request: Request,
    body: DecisionsRequest,
    *,
    principal: Any,
    session: Any,
    settings: Any,
    quota: Any,
    redactor: Any,
    providers: Any,
    estimator: Any,
) -> JSONResponse:
    fmt = _validate(body, route)

    request_id = _metered.request_id_for(request, settings)
    plan = await _metered.resolve_plan(
        session,
        body.model,
        principal=principal,
        surface=SURFACE,
        affinity=affinity_key(request.headers, body),
        fx=request.app.state.fx,
        currency=settings.billing_currency,
        catalogue=request.app.state.catalogue,
    )

    async def serve(resolved: _metered.ResolvedModel) -> JSONResponse:
        model = resolved.model
        # Before any accounting row exists, so a refusal leaves nothing
        # in-progress behind: a provider that cannot serve decisions, and a
        # body that carries the deployment's routing settings.
        support = require_decision_support(model.provider, model.name)
        path = support.paths.get(route)
        if path is None:
            raise BadRequestError(
                f"The provider behind model {model.name!r} does not serve /v1/{route}.",
                code="provider_no_decision_support",
            )
        payload: dict[str, Any] = body.model_dump(exclude_unset=True)
        _refuse_routing_fields(payload, support, model)
        upstream = await _metered.resolve_upstream(providers, resolved)

        # -- redaction ----------------------------------------------------------
        text_field = "input" if fmt == "input" else "state"
        texts = list(_strings(payload[text_field]))
        outcome = await _metered.redact_or_block(
            request,
            [{"role": "user", "content": text} for text in texts],
            redactor=redactor,
            session=session,
            principal=principal,
            model=model,
            settings=settings,
            surface=SURFACE,
            request_id=request_id,
        )
        redacted = iter(str(message.get("content") or "") for message in outcome.messages)
        payload[text_field] = _rebuild(payload[text_field], redacted)

        # -- reservation --------------------------------------------------------
        # The provider tokenises the questions as well as the text (a 3-question
        # request about a one-line text measured 143 input tokens), so both are
        # counted. A figure of the right order is the point: the real count is
        # settled from the provider's usage.
        prompt_tokens = estimator.count_text(
            orjson.dumps(
                {text_field: payload[text_field], "questions": payload["questions"]}
            ).decode()
        )
        _metered.enforce_input_limit(model, prompt_tokens)
        metered = await _metered.begin(
            fx=request.app.state.fx,
            session_factory=request.app.state.session_factory,
            session=session,
            principal=principal,
            settings=settings,
            quota=quota,
            estimator=estimator,
            resolved=resolved,
            surface=SURFACE,
            request_id=request_id,
            worst_case=TokenCounts(prompt=prompt_tokens, completion=0),
            outcome=outcome,
            streamed=False,
        )
        if isinstance(metered, JSONResponse):
            return metered

        payload["model"] = resolved.upstream_model
        payload = metered.shape_payload(payload, surface=SURFACE)

        try:
            response = await upstream.decisions(payload, path=path, request_id=request_id)
        except UpstreamError as exc:
            raise await metered.upstream_unreachable(exc) from exc

        if response.status_code >= 400:
            return await metered.upstream_refused(
                status_code=response.status_code,
                message=_metered.error_text(response.payload),
                content=response.payload,
            )

        if response.payload is not None:
            # Usage, `usage.cost` for the plugin to read, and the model that
            # served it; the provider and its model also arrive in headers.
            metered.accounting.observe_payload(response.payload, headers=response.headers)

        body_out = dict(response.payload or {})
        # Our name for the model, as every route does: the upstream's is in the
        # ledger. A System One response may carry none (null), which stays.
        if isinstance(body_out.get("model"), str):
            body_out["model"] = resolved.requested_name
        return JSONResponse(
            status_code=response.status_code,
            content=body_out,
            background=metered.completed_after_response(upstream_status=response.status_code),
        )

    return await _metered.serve_with_failover(session, plan, serve)


@router.post("/decisions", response_model=None)
async def create_decisions(
    request: Request,
    body: DecisionsRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse:
    return await _decide(
        "decisions",
        request,
        body,
        principal=principal,
        session=session,
        settings=settings,
        quota=quota,
        redactor=redactor,
        providers=providers,
        estimator=estimator,
    )


@router.post("/systemone", response_model=None)
async def create_systemone(
    request: Request,
    body: DecisionsRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse:
    return await _decide(
        "systemone",
        request,
        body,
        principal=principal,
        session=session,
        settings=settings,
        quota=quota,
        redactor=redactor,
        providers=providers,
        estimator=estimator,
    )
