"""Text out of documents, so that something can read them.

Two callers want this and they want different things from it, which is why the
result is a structured outcome and not a string:

* **Redaction** needs to know whether an attachment carries personal data. For
  it, "we could not read this" is a different answer from "there was nothing in
  it", and conflating them is how a photographed passport passes as clean.
* **Extraction as a service** — the `/v1/ocr` surface — needs the text itself,
  for a caller that is going to embed it, index it, or feed it to a model.

Three responsibilities, and the second is the one to keep hold of:

**Extraction only, never interpretation.** These functions return text and say
where it came from. Whether a document may be forwarded, embedded or refused is
a policy decision about a caller, and it belongs to whoever called this.

**"No text" is not "no PII".** A scan is a picture of a page: no text layer,
nothing extracted, and nothing is indistinguishable from a clean document unless
the difference is reported. ``Extraction.kind`` reports it, and
``Extraction.inspected`` is the single predicate a caller should branch on.
Reading scans needs OCR, which is a different engine and an admin's decision.

**Untrusted input, kept away from the secrets.** This parses hostile bytes: zip
bombs, OOXML with a billion laughs, PDFs that decompress to gigabytes. It runs
in a service that holds no database credential and no ``SecretBox`` key, for the
same reason spaCy does — the process that must not fall over is the gateway. The
limits below are that decision, not tuning.

## Why markitdown

One entry point over four format libraries: a single upstream to track, one API,
and no per-format branch here to keep in step with each library's quirks. MIT,
checked at source on 2026-09-06 (0.1.7). The extras are named rather than taking
``[all]``, which would add Azure clients, YouTube transcripts and audio
transcription to a PII detector, and ``[xls]`` earns its place by covering the
pre-2007 OLE format that the per-format libraries cannot read at all.

Two things are pinned deliberately when constructing it:

* ``enable_plugins=False`` — markitdown loads third-party converter plugins from
  the environment when asked to. A PII detector is the last place to let an
  installed package register itself into the parse path.
* an explicit ``StreamInfo`` — markitdown otherwise sniffs the type with magika,
  an ML classifier. Passing the type the caller declared keeps the decision with
  the caller and out of a model's hands; the alternative is a document whose
  handling depends on what a classifier guessed it was.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Final

from llmp_shared import ExtractionKind, ExtractionResponse

logger = logging.getLogger(__name__)

#: Refused before anything is parsed. A document larger than this is not
#: inspected, and therefore must not be treated as inspected: the caller gets
#: `too_large` and decides.
MAX_BYTES: Final = 25 * 1024 * 1024

#: Stop after this much text. Detection cost is linear in length
#: (docs/performance.md), so an unbounded document is an unbounded stall for
#: every request sharing the process.
MAX_CHARS: Final = 2_000_000

#: Per-member limit inside an OOXML archive, read from the central directory
#: before anything is decompressed. The whole point of a zip bomb is that it is
#: small until you open it.
MAX_MEMBER_BYTES: Final = 100 * 1024 * 1024


_DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX: Final = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_DOC: Final = "application/msword"
_XLS: Final = "application/vnd.ms-excel"
_PDF: Final = "application/pdf"

#: Accepted types, mapped to the extension markitdown dispatches on. An
#: allowlist rather than "whatever markitdown supports": its converter set
#: includes URLs, feeds and audio, and a document extractor that quietly
#: fetches a YouTube transcript because someone posted a link is not the
#: surface anyone asked for.
_ACCEPTED: Final[dict[str, str]] = {
    _DOCX: ".docx",
    _XLSX: ".xlsx",
    _PPTX: ".pptx",
    _DOC: ".doc",
    _XLS: ".xls",
    _PDF: ".pdf",
}

#: Only consulted when the declared type is missing or unrecognised: several
#: clients send `application/octet-stream` for every upload.
_BY_EXTENSION: Final[dict[str, str]] = {
    ".docx": _DOCX,
    ".xlsx": _XLSX,
    ".pptx": _PPTX,
    ".doc": _DOC,
    ".xls": _XLS,
    ".pdf": _PDF,
}

#: The OOXML types are zip archives and get the bomb guard. The OLE formats and
#: PDF are not zips, so opening them as one would fail for the wrong reason.
_ZIP_BACKED: Final = frozenset({_DOCX, _XLSX, _PPTX})

SUPPORTED_MEDIA_TYPES: Final = tuple(sorted(_ACCEPTED))


def media_type_for(media_type: str | None, filename: str | None) -> str | None:
    """The type to extract as, from what the client claimed.

    A recognised declared type wins. Otherwise the extension is consulted,
    because `application/octet-stream` named `.docx` is a document and refusing
    it helps nobody. A filename never overrides a type we recognise: that is how
    a PDF called `.docx` becomes parser confusion.
    """
    declared = (media_type or "").split(";")[0].strip().lower()
    if declared in _ACCEPTED:
        return declared
    name = (filename or "").lower()
    for extension, mapped in _BY_EXTENSION.items():
        if name.endswith(extension):
            return mapped
    return None


def _clip(text: str, extractor: str) -> ExtractionResponse:
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]
    if not text.strip():
        return ExtractionResponse(kind=ExtractionKind.NO_TEXT_LAYER, extractor=extractor)
    return ExtractionResponse(
        kind=ExtractionKind.TEXT, text=text, extractor=extractor, truncated=truncated
    )


def _zip_problem(data: bytes) -> str | None:
    """Refuse an archive whose declared contents are absurd, before opening it."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.file_size > MAX_MEMBER_BYTES:
                    return f"a member declares {info.file_size} bytes"
    except zipfile.BadZipFile as exc:
        return f"not a readable archive: {exc}"
    return None


def _pdf_pages(data: bytes) -> int:
    """How many pages the PDF declares, or 0 if it will not say.

    Counted because the OCR surface bills and reports per page, and a page count
    invented at that layer would be a number with no source. Only PDFs have one:
    a spreadsheet has no pages until something decides on a paper size, so those
    report 0 rather than 1, which would be a guess dressed as a measurement.
    """
    try:
        from pdfminer.pdfpage import PDFPage

        return sum(1 for _ in PDFPage.get_pages(io.BytesIO(data)))
    except Exception as exc:
        logger.debug("could not count PDF pages: %s", type(exc).__name__)
        return 0


def _converter() -> object:
    """One markitdown instance, built with the two settings that matter.

    Constructed per call rather than cached: it is cheap, and a converter that
    holds no state cannot leak one document's context into the next.
    """
    from markitdown import MarkItDown

    return MarkItDown(enable_builtins=True, enable_plugins=False)


def extract(
    data: bytes, *, media_type: str | None = None, filename: str | None = None
) -> ExtractionResponse:
    """Text from one document, with why it is what it is.

    Never raises for bad input. A document that cannot be read is a fact to
    report, because the caller has to answer "so do we forward this or not"
    either way, and an exception makes that answer "500".
    """
    if len(data) > MAX_BYTES:
        return ExtractionResponse(
            kind=ExtractionKind.TOO_LARGE,
            detail=f"{len(data)} bytes exceeds the {MAX_BYTES}-byte inspection limit",
        )

    resolved = media_type_for(media_type, filename)
    if resolved is None:
        return ExtractionResponse(
            kind=ExtractionKind.UNSUPPORTED,
            detail=f"no extractor for {media_type or 'an unnamed type'}",
        )

    if resolved in _ZIP_BACKED and (problem := _zip_problem(data)):
        return ExtractionResponse(kind=ExtractionKind.UNREADABLE, detail=problem)

    from markitdown import StreamInfo, UnsupportedFormatException

    stream_info = StreamInfo(mimetype=resolved, extension=_ACCEPTED[resolved])
    try:
        result = _converter().convert_stream(io.BytesIO(data), stream_info=stream_info)  # type: ignore[attr-defined]
    except UnsupportedFormatException as exc:
        # Accepted by our allowlist, declined by the library — a missing extra,
        # or a format it dropped. Distinct from "we do not accept this type".
        logger.warning("markitdown declined %s: %s", resolved, type(exc).__name__)
        return ExtractionResponse(
            kind=ExtractionKind.UNSUPPORTED, detail=f"the extractor cannot read {resolved}"
        )
    except Exception as exc:
        # Only the exception *type* is logged. A parser's message can quote the
        # bytes it choked on, and those bytes are the document.
        logger.warning("could not extract %s: %s", resolved, type(exc).__name__)
        return ExtractionResponse(
            kind=ExtractionKind.UNREADABLE,
            detail=f"{type(exc).__name__} while reading {resolved}",
        )

    outcome = _clip(result.text_content or "", "markitdown")
    if resolved == _PDF:
        # Counted even when no text came out: a scanned PDF has pages, and the
        # caller deciding what to do about `no_text_layer` wants to know whether
        # it is one page or two hundred.
        return outcome.model_copy(update={"pages": _pdf_pages(data)})
    return outcome
