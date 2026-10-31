"""``POST /v1/audio/transcriptions`` and ``/v1/audio/translations``.

OpenAI's multipart shape, because it is the one every speech client already
speaks: `file`, `model`, and optionally `language`, `prompt`, `response_format`
(`json`, `text`, `verbose_json`, `srt`, `vtt`), `temperature` and
`timestamp_granularities[]`. The same pipeline as every other metered route
(resolve, check access, reserve, call, record, settle), with the differences
that come from the shape of the operation:

**The unit is time.** Not a token and not a page. Cost comes from
`model_prices.per_minute` and the seconds of audio (see `gateway.audio` for
where the figure comes from, in order: the provider's own reported duration,
the file's header, a size estimate that the ledger labels as one).

**The reservation is exact when it can be.** The duration is read from the file
before the call, so the quota reservation is the real figure for every format
`tinytag` and `wave` understand. Only a file neither can read reserves a
pessimistic bound from its size (8 kbit/s), and settles to the real figure.

**Redaction cannot run.** Audio is bytes: there is nothing to detect in before
it leaves, and a transcript has no placeholders to restore. The route does not
pretend otherwise; it offers an operator the one honest control,
`transcription.allowed_under_redaction`, which refuses a caller for whom an
active redaction rule is in force (docs/redaction.md).

**Never streamed, bounded.** A size cap (413) and a timeout, both settings. A
partial transcript is not billable output.

Translations (`/audio/translations`, speech to English text) share the handler:
same kind, same unit, same upload, and no `language` field.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from gateway import audio
from gateway.accounting import TokenCounts
from gateway.config import EffectivePolicy, EntityMode, RedactionPolicy, Settings
from gateway.deps import (
    EstimatorDep,
    PrincipalDep,
    ProvidersDep,
    QuotaDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, PayloadTooLargeError, PermissionError_
from gateway.models import ApiSurface
from gateway.routers import _metered
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.TRANSCRIPTION

RESPONSE_FORMATS = ("json", "text", "verbose_json", "srt", "vtt")

#: Multipart framing and the small text fields, on top of the file itself, when
#: judging the Content-Length of the whole request against the file cap.
_OVERHEAD_BYTES = 64 * 1024

_MULTIPART_SCHEMA: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file", "model"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "model": {"type": "string"},
                        "language": {"type": "string"},
                        "prompt": {"type": "string"},
                        "response_format": {"type": "string", "enum": list(RESPONSE_FORMATS)},
                        "temperature": {"type": "number"},
                        "timestamp_granularities[]": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["word", "segment"]},
                        },
                    },
                }
            }
        },
    }
}


def redaction_in_force(effective: EffectivePolicy | None, settings: Settings) -> bool:
    """Whether a redaction rule that changes text applies to this caller.

    ``off`` means detected and left alone, so a policy whose every mode is off
    is not in force. A deployment on the ``noop`` engine redacts nothing
    whatever its rules say.
    """
    if settings.redaction.engine == "noop":
        return False
    policy: RedactionPolicy = effective.policy if effective else settings.redaction.policy
    if policy.default_mode is not EntityMode.OFF:
        return True
    if any(entry.mode is not EntityMode.OFF for entry in policy.entities.values()):
        return True
    return any(pattern.mode is not EntityMode.OFF for pattern in policy.patterns)


def _text(form: Any, name: str) -> str | None:
    value = form.get(name)
    if value is None or isinstance(value, UploadFile):
        return None
    value = str(value).strip()
    return value or None


@router.post("/audio/transcriptions", response_model=None, openapi_extra=_MULTIPART_SCHEMA)
async def transcribe(
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> Response:
    return await _serve(
        request,
        principal=principal,
        session=session,
        settings=settings,
        quota=quota,
        providers=providers,
        estimator=estimator,
        upstream_path="/audio/transcriptions",
        translate=False,
    )


@router.post("/audio/translations", response_model=None, openapi_extra=_MULTIPART_SCHEMA)
async def translate(
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> Response:
    return await _serve(
        request,
        principal=principal,
        session=session,
        settings=settings,
        quota=quota,
        providers=providers,
        estimator=estimator,
        upstream_path="/audio/translations",
        translate=True,
    )


async def _serve(
    request: Request,
    *,
    principal: Any,
    session: Any,
    settings: Settings,
    quota: Any,
    providers: Any,
    estimator: Any,
    upstream_path: str,
    translate: bool,
) -> Response:
    cap = settings.transcription.max_upload_bytes
    too_large = PayloadTooLargeError(
        f"The audio file is larger than this deployment accepts ({cap // (1024 * 1024)} MB "
        f"at most, GATEWAY_TRANSCRIPTION__MAX_UPLOAD_BYTES={cap})."
    )
    # Before the body is parsed at all: a declared length over the cap is
    # refused without spooling the upload to disk first.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + _OVERHEAD_BYTES:
        raise too_large

    # The form is closed as soon as the bytes are copied out of it: starlette
    # spools an upload to a temporary file, and leaving it to the garbage
    # collector is a descriptor held for as long as the provider takes.
    form = await request.form()
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise BadRequestError("'file' is required and must be an uploaded audio file.")
        model_name = _text(form, "model")
        if model_name is None:
            raise BadRequestError("'model' is required.")
        response_format = _text(form, "response_format") or "json"
        if response_format not in RESPONSE_FORMATS:
            raise BadRequestError(
                f"'response_format' must be one of {', '.join(RESPONSE_FORMATS)}.",
                code="invalid_response_format",
            )
        if (_text(form, "stream") or "").lower() == "true":
            raise BadRequestError(
                "Streaming is not supported on this route.", code="stream_unsupported"
            )
        temperature = _text(form, "temperature")
        if temperature is not None:
            try:
                float(temperature)
            except ValueError:
                raise BadRequestError("'temperature' must be a number.") from None

        data = await upload.read(cap + 1)
        if len(data) > cap:
            raise too_large
        if not data:
            raise BadRequestError("The audio file is empty.", code="empty_file")
    finally:
        await form.close()

    request_id = _metered.request_id_for(request, settings)
    resolved = await _metered.resolve_model(
        session, model_name, principal=principal, surface=SURFACE
    )
    model = resolved.model
    upstream = await _metered.resolve_upstream(providers, resolved)

    if not settings.transcription.allowed_under_redaction and redaction_in_force(
        _metered.redaction_policy(request, principal=principal, model=model), settings
    ):
        raise PermissionError_(
            "A redaction rule applies to this request, and audio cannot be screened "
            "before it reaches the provider. This deployment does not allow transcription "
            "under redaction.",
            code="transcription_not_allowed_under_redaction",
        )

    # Read before the call so the reservation is the real figure whenever the
    # container can be parsed; the size bound is only for files it cannot.
    measured = audio.measure(data)
    reserved = measured or audio.estimate(
        len(data), bytes_per_second=audio.RESERVE_BYTES_PER_SECOND
    )
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
        worst_case=TokenCounts(audio_seconds=reserved),
        outcome=None,
        streamed=False,
    )
    if isinstance(metered, JSONResponse):
        return metered

    fields: list[tuple[str, str]] = [
        ("model", resolved.upstream_model),
        ("response_format", response_format),
    ]
    if not translate and (language := _text(form, "language")):
        fields.append(("language", language))
    if prompt := _text(form, "prompt"):
        fields.append(("prompt", prompt))
    if temperature is not None:
        fields.append(("temperature", temperature))
    fields.extend(
        ("timestamp_granularities[]", str(value))
        for value in form.getlist("timestamp_granularities[]")
        if not isinstance(value, UploadFile)
    )

    try:
        response = await upstream.transcribe(
            upstream_path,
            fields,
            filename=upload.filename or "audio",
            content=data,
            content_type=upload.content_type or "application/octet-stream",
            timeout_seconds=settings.transcription.timeout_seconds,
            request_id=request_id,
        )
    except UpstreamError as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if response.status_code >= 400:
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=_metered.error_text(response.payload)
            or response.raw.decode("utf-8", "replace")[:300]
            or None,
            content=response.payload
            or {"error": {"message": "The upstream provider returned an error."}},
        )

    if response.payload is not None:
        metered.accounting.observe_payload(response.payload, headers=response.headers)

    duration = _duration(response.payload, measured, len(data))
    metered.accounting.observe_audio(duration.seconds, duration.source)

    background = metered.completed_after_response(upstream_status=response.status_code)
    if response.payload is not None:
        return JSONResponse(
            status_code=response.status_code, content=response.payload, background=background
        )
    return Response(
        content=response.raw,
        status_code=response.status_code,
        media_type=response.headers.get("content-type") or "text/plain; charset=utf-8",
        background=background,
    )


def _duration(
    payload: dict[str, Any] | None, measured: Decimal | None, size: int
) -> audio.Duration:
    """The billable seconds, from the best source there is."""
    if (seconds := audio.reported(payload)) is not None:
        return audio.Duration(seconds, audio.UPSTREAM)
    if measured is not None:
        return audio.Duration(measured, audio.MEASURED)
    return audio.Duration(audio.estimate(size), audio.ESTIMATED)
