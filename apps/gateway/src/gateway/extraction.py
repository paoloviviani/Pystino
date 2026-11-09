"""Calling the local extraction service, and shaping its answer as OCR.

One hop and one translation. The service's contract is deliberately not the OCR
API's — `llmp_shared.documents` is bytes in, text and a reason out — because it
serves redaction too, and a detector has no use for `pages[]`. Translating here
keeps `/v1/ocr` one surface with two backends instead of two surfaces that
happen to resemble each other.

What the translation refuses to invent, which is the whole reason it is a
function with a docstring rather than a dict comprehension:

* **Pagination.** markitdown returns one document's text, not per-page text. A
  `.docx` has no pages until something renders it, so the response carries a
  single page. Splitting the markdown at a guess and numbering the pieces would
  produce a `pages[]` array that looks like the real thing and means nothing.
* **`images`, bounding boxes and confidence scores.** Absent rather than
  present-and-empty: an empty `images` array says "this page had no pictures",
  which is a claim nobody made.
* **A page count for formats without pages.** `usage_info.pages_processed` is
  the PDF's real page count where there is one and zero otherwise — never one,
  which would be a billing figure invented to look tidy.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from llmp_shared import ExtractionKind, ExtractionResponse

from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)


#: How long one document may take when the caller does not say. Mirrors
#: `ExtractorSettings.timeout_seconds`, which is what the route passes.
DEFAULT_TIMEOUT_SECONDS = 180.0

#: The provider test's own, much shorter budget: its document is one built-in page.
PROBE_TIMEOUT_SECONDS = 30.0

#: Connecting to a compose-network service is quick or it is not there at all;
#: this is not the slow part, so it is not stretched with the read.
_CONNECT_TIMEOUT_SECONDS = 10.0


#: The built-in extractor's models and the reader each one means (docs/extractor-engines.md).
#: The key is the model's upstream name; the value is the ``X-Extractor-Engine`` the
#: gateway sends. Each name means exactly one engine, whatever the extractor image
#: defaults to: ``markitdown`` is the plain reader (no OCR), ``markitdown-ocr`` adds
#: OCR, ``docling`` is Docling. ``EXTRACTOR_ENGINE`` decides only a call with no header,
#: which the gateway makes for an extractor model it does not know (an operator's own row).
ENGINE_OF_MODEL: dict[str, str] = {
    "markitdown": "markitdown",
    "markitdown-ocr": "ocr",
    "docling": "docling",
}
#: The plain reader's model: always offered, since every build has markitdown.
DEFAULT_MODEL = "markitdown"


def engine_of_model(upstream_model: str | None) -> str | None:
    """The engine header a built-in extractor model sends; None for a model this build
    does not know (no header: the extractor's configured engine reads)."""
    return ENGINE_OF_MODEL.get(upstream_model or "")


#: How long a ``/healthz`` answer is trusted. Engines change when the image
#: does, so a minute-old answer is as good as a fresh one, and listings and
#: pickers must not each cost a round trip.
_ENGINES_TTL_SECONDS = 60.0
_ENGINES_FAILED_TTL_SECONDS = 5.0
_engines_cache: dict[str, tuple[float, frozenset[str] | None]] = {}


async def reported_engines(client: httpx.AsyncClient, endpoint: str) -> frozenset[str] | None:
    """The engines the extractor at *endpoint* says it can run, or None when it cannot be asked.

    ``/healthz``'s ``extractor_engines``. A service that predates the field
    reports markitdown only, which is true of every older image's *offering*
    (the others were reached by header and silently fell back). Never raises;
    an unreachable service is None, cached briefly so an outage is not a storm.
    """
    now = time.monotonic()
    held = _engines_cache.get(endpoint)
    if held is not None and held[0] > now:
        return held[1]
    found: frozenset[str] | None
    try:
        response = await client.get(f"{endpoint.rstrip('/')}/healthz", timeout=5.0)
        response.raise_for_status()
        body = response.json()
        listed = body.get("extractor_engines") if isinstance(body, dict) else None
        found = (
            frozenset(str(e) for e in listed)
            if isinstance(listed, list)
            else frozenset({"markitdown"})
        )
    except Exception as exc:
        logger.info("could not ask the extractor for its engines: %s", exc)
        found = None
    ttl = _ENGINES_TTL_SECONDS if found is not None else _ENGINES_FAILED_TTL_SECONDS
    _engines_cache[endpoint] = (now + ttl, found)
    return found


def forget_reported_engines() -> None:
    """Drop the cache (tests, and a provider's base URL changing)."""
    _engines_cache.clear()


def model_availability(upstream_model: str | None, engines: frozenset[str] | None) -> bool:
    """Whether a built-in extractor model can run.

    ``markitdown`` and a model this build does not know are always True (every build
    has markitdown; the unknown ones are the operator's). Another engine is True only
    when the extractor reports it: an extractor that cannot be asked is False, so a
    model is never offered or used on a guess (fail closed).
    """
    engine = engine_of_model(upstream_model)
    if engine is None or engine == "markitdown":
        return True
    return engines is not None and engine in engines


class ExtractionTimeout(UpstreamError):
    """The extraction service did not answer within the document timeout.

    A subclass of `UpstreamError` so every existing handler still treats it as
    "the extractor could not be reached"; the route reads the type to give the
    caller a message that says what to change.
    """


class DocumentRefused(Exception):
    """The document was not read, and the caller needs to know why.

    Carries the reason as something actionable rather than a bare failure: "this
    reads text layers, and yours is a scan" tells someone which model to ask
    for, where "extraction failed" sends them to a support channel.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


#: What each non-text outcome means for the caller. The messages name the next
#: step, because every one of these is a thing the caller can act on.
_REFUSALS: dict[ExtractionKind, tuple[str, str]] = {
    ExtractionKind.NO_TEXT_LAYER: (
        "no_text_layer",
        "This document has no text layer — it is a scan or a set of images. "
        "The local extractor reads text, not pictures of text; use an OCR "
        "model for this document.",
    ),
    ExtractionKind.UNSUPPORTED: (
        "unsupported_document",
        "The local extractor does not read this format. It handles Word "
        "(.docx and legacy .doc), Excel (.xlsx and .xls), PowerPoint (.pptx) and PDF.",
    ),
    ExtractionKind.TOO_LARGE: (
        "document_too_large",
        "This document is larger than the extractor will inspect.",
    ),
    ExtractionKind.UNREADABLE: (
        "unreadable_document",
        "The document could not be opened. It may be corrupt, encrypted, or "
        "not the format it claims to be.",
    ),
}


async def extract_document(
    client: httpx.AsyncClient,
    endpoint: str,
    data: bytes,
    *,
    media_type: str | None,
    filename: str | None = None,
    request_id: str | None = None,
    page_images: int = 0,
    long_side: int | None = None,
    engine: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ExtractionResponse:
    """Ask the extraction service to read one document.

    The body *is* the document, with its own content type — the shape the
    service serves. Raises `UpstreamError` for a service that cannot be reached,
    which the route turns into the same 502 an unreachable provider produces:
    from the caller's side a local extractor that is down and a remote one that
    is down are the same event.

    ``timeout_seconds`` bounds the whole wait for the answer and is applied per
    request, so it holds whatever the shared client's own defaults are (the
    control-plane client's is 10 s, far too short for a large PDF). Past it,
    `ExtractionTimeout` is raised and a WARN names the setting to raise.
    """
    headers = {"content-type": media_type or "application/octet-stream"}
    if filename:
        headers["x-filename"] = filename
    if request_id:
        headers["x-request-id"] = request_id
    if engine:
        # The reader a routing rule chose for this one document; an engine the
        # service does not know is ignored there, not refused.
        headers["x-extractor-engine"] = engine
    if page_images > 0:
        headers["x-page-images"] = str(page_images)
        if long_side:
            headers["x-page-image-long-side"] = str(long_side)

    started = time.monotonic()
    try:
        response = await client.post(
            f"{endpoint.rstrip('/')}/extract",
            content=data,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        return ExtractionResponse.model_validate(response.json())
    except httpx.TimeoutException as exc:
        elapsed = time.monotonic() - started
        logger.warning(
            "document extraction timed out after %.1fs (limit %.0fs, %d bytes, %s); "
            "raise GATEWAY_EXTRACTOR__TIMEOUT_SECONDS to allow larger documents",
            elapsed,
            timeout_seconds,
            len(data),
            media_type or "unknown type",
        )
        raise ExtractionTimeout(
            f"the extraction service did not answer within {timeout_seconds:.0f}s "
            "(GATEWAY_EXTRACTOR__TIMEOUT_SECONDS)",
            cause=exc,
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamError(f"the extraction service could not be reached: {exc}") from exc


#: The caps on what the gateway will ask the extractor to draw.
MAX_PAGE_IMAGES = 50
MAX_LONG_SIDE = 2048


def as_ocr_response(
    outcome: ExtractionResponse, *, model_name: str, want_page_images: bool = False
) -> dict[str, Any]:
    """The extraction outcome in the OCR response shape.

    Raises `DocumentRefused` for anything that was not read. That is the design
    decision worth knowing: a document the extractor could not read comes back
    as an **error with a reason**, never as a successful response with an empty
    `pages` array — because an empty success is indistinguishable from a blank
    document, and a caller that indexes it has silently indexed nothing.
    """
    if want_page_images and outcome.page_images:
        # A PDF with pages that are pictures, and a caller that can read them:
        # a 200 even when no page had text (the caller asked for exactly this),
        # never the 422 below. `pages` has one entry per PDF page in order — text
        # pages as today, picture pages with empty markdown — and the pictures
        # are keyed by 1-based page number. `pages_processed` is what was done:
        # pages read as text plus pages drawn.
        texts = outcome.page_texts or [outcome.text]
        return {
            "model": model_name,
            "pages": [{"index": i, "markdown": text} for i, text in enumerate(texts)],
            "usage_info": {
                "pages_processed": sum(1 for text in texts if text) + len(outcome.page_images)
            },
            "extractor": outcome.extractor,
            "page_images": [image.model_dump() for image in outcome.page_images],
            "page_count": outcome.pages,
            "truncated": outcome.images_truncated,
        }
    if outcome.kind is not ExtractionKind.TEXT:
        code, message = _REFUSALS.get(
            outcome.kind, ("document_not_read", "The document could not be read.")
        )
        if outcome.detail:
            # The extractor knows *why* — "old PowerPoint .ppt is not supported;
            # save as .pptx" — and the generic sentence above does not. Dropping
            # it sent callers to guess which of the five causes it was. It is
            # safe to forward: the service never puts document content in it.
            reason = outcome.detail.strip().rstrip(".")
            if outcome.kind is ExtractionKind.UNSUPPORTED:
                message = f"The local extractor cannot read this document: {reason}."
            else:
                message = f"{message} Reason: {reason}."
        raise DocumentRefused(message, code=code)

    page: dict[str, Any] = {"index": 0, "markdown": outcome.text}
    return {
        "model": model_name,
        "pages": [page],
        "usage_info": {
            # The document's own count where the format has one, zero where it
            # does not. Not the length of the text, and not one-for-tidiness.
            "pages_processed": outcome.pages,
        },
        # Ours, not the OCR API's: a caller comparing two extractors wants to
        # know which read this, and the ledger records it for the same reason.
        "extractor": outcome.extractor,
        **({"truncated": True} if outcome.truncated else {}),
    }
