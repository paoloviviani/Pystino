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
from typing import Any

import httpx
from llmp_shared import ExtractionKind, ExtractionResponse

from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)


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
        "The local extractor does not read this format. It handles Word, "
        "Excel, PowerPoint and PDF.",
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
) -> ExtractionResponse:
    """Ask the extraction service to read one document.

    The body *is* the document, with its own content type — the shape the
    service serves. Raises `UpstreamError` for a service that cannot be reached,
    which the route turns into the same 502 an unreachable provider produces:
    from the caller's side a local extractor that is down and a remote one that
    is down are the same event.
    """
    headers = {"content-type": media_type or "application/octet-stream"}
    if filename:
        headers["x-filename"] = filename
    if request_id:
        headers["x-request-id"] = request_id

    try:
        response = await client.post(
            f"{endpoint.rstrip('/')}/extract", content=data, headers=headers
        )
        response.raise_for_status()
        return ExtractionResponse.model_validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamError(f"the extraction service could not be reached: {exc}") from exc


def as_ocr_response(outcome: ExtractionResponse, *, model_name: str) -> dict[str, Any]:
    """The extraction outcome in the OCR response shape.

    Raises `DocumentRefused` for anything that was not read. That is the design
    decision worth knowing: a document the extractor could not read comes back
    as an **error with a reason**, never as a successful response with an empty
    `pages` array — because an empty success is indistinguishable from a blank
    document, and a caller that indexes it has silently indexed nothing.
    """
    if outcome.kind is not ExtractionKind.TEXT:
        code, message = _REFUSALS.get(
            outcome.kind, ("document_not_read", "The document could not be read.")
        )
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
