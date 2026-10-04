"""The extraction contract: bytes in, text and a reason out.

The sibling of the detection contract, and deliberately the same shape of
promise — a service that serves this can be replaced by another that serves it,
and the gateway does not learn what read the document (ADR 0026's argument,
applied to a second capability).

The field that carries the weight is ``kind``. Extraction has more outcomes than
"here is the text": a scan has no text to give, a corrupt file cannot be opened,
an oversized one is refused unread. Every one of those returns an empty string,
so a caller that branches on the text rather than the kind will treat all of
them as "this document is clean" — which, for the caller that is deciding
whether to forward an attachment, is the one wrong answer.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ExtractionKind(StrEnum):
    """Why the text is what it is."""

    #: Text was extracted and is in ``text``.
    TEXT = "text"
    #: Opened, and there was no text in it: a scanned page, or an empty
    #: document. Reading it would need OCR, which is a different engine.
    NO_TEXT_LAYER = "no_text_layer"
    #: A format this service does not accept.
    UNSUPPORTED = "unsupported"
    #: Refused on size before parsing.
    TOO_LARGE = "too_large"
    #: Opened and failed: corrupt, encrypted, or not what it claimed to be.
    UNREADABLE = "unreadable"


class PageImage(BaseModel):
    """One rendered page of a document that has no text layer."""

    #: 1-based, as a person counts pages.
    page: int = Field(ge=1)
    mime: str
    #: The encoded image, base64.
    data: str


class ExtractionResponse(BaseModel):
    """What one document yielded."""

    kind: ExtractionKind
    text: str = ""
    #: What read it, for the same reason ``DetectionResponse.engine`` exists: an
    #: operator asking why a document passed needs to know what read it.
    extractor: str = ""
    #: Why, when ``kind`` is a failure. Never quotes document content, because
    #: the content is the thing being protected.
    detail: str = ""
    #: True when the text was cut at the service's limit, so a caller can say
    #: that what it saw was not all of it.
    truncated: bool = False
    #: How many pages the extractor believes it read. Zero when the format has
    #: no pages — a spreadsheet does not — rather than a guess.
    pages: int = Field(default=0, ge=0)
    #: Text per page of a PDF that was read page by page, empty for a page that
    #: is a picture. Only set when the caller asked for page images.
    page_texts: list[str] = Field(default_factory=list)
    #: Pages that had no text of their own, drawn as pictures, only when the
    #: caller asked for them. Drawing a page is not reading it: such a document
    #: is not "inspected", and a model that looks at the pictures is the reader.
    page_images: list[PageImage] = Field(default_factory=list)
    #: True when more pages were pictures than ``page_images`` holds — cut at the
    #: page cap or the payload cap — so a caller can say it saw only some.
    images_truncated: bool = False

    @property
    def inspected(self) -> bool:
        """Whether the document was actually read.

        The predicate to branch on. ``kind is TEXT`` and nothing else: every
        other outcome means the content was not seen, and "not seen" must never
        be reported to a user as "nothing found". A PDF with picture pages is
        partly unread even though its other pages were text.
        """
        return self.kind is ExtractionKind.TEXT and not self.page_images
