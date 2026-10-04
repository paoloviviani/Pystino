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

## What the bytes say beats what the client said

The container format is read from the file's first bytes, not from the declared
type or the extension. The case that forced this was an old binary `.doc` saved
as `menu.doc.docx`: declared Word 2007, actually an OLE2 compound file, which
the OOXML reader rejects as "not a zip". Bytes decide between ZIP (OOXML) and
CFB (OLE2, the pre-2007 formats and password-protected OOXML); the declared type
only breaks ties among formats that share a container, and is the only signal
for PDF.

Legacy Word `.doc` has no markitdown converter at all, so it goes to `antiword`
(GPL-2.0, run as a separate process rather than linked, and installed by the
image rather than by pip). Pre-2007 Excel is read by markitdown through xlrd.
Old PowerPoint `.ppt` and password-protected Office files are refused, each with
a reason that names the format and the next step.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import zipfile
from enum import StrEnum
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


#: The two containers an Office file can arrive in.
_ZIP_MAGIC: Final = b"PK\x03\x04"
_CFB_MAGIC: Final = bytes.fromhex("d0cf11e0a1b11ae1")

#: antiword is a C program parsing hostile bytes; a wall-clock limit is the only
#: defence against an input that makes it spin. Generous for a 25 MB document.
ANTIWORD_TIMEOUT_SECONDS: Final = 30

#: Rendering a legacy .doc to UTF-8 needs antiword's own mapping file, which
#: the Debian package installs. Without `-m` it prints ISO-8859-1 on a POSIX
#: locale and every accent in an Italian document becomes mojibake downstream.
_ANTIWORD_MAPPING: Final = "UTF-8.txt"

ANTIWORD_MISSING: Final = "Old Word .doc files need antiword in the extractor image"


class _Container(StrEnum):
    ZIP = "zip"
    CFB = "cfb"


class _Legacy(StrEnum):
    """What a CFB file is, from the streams at the root of the container."""

    WORD = "word"
    EXCEL = "excel"
    POWERPOINT = "powerpoint"
    ENCRYPTED = "encrypted"
    OTHER = "other"


def _container(data: bytes) -> _Container | None:
    if data.startswith(_CFB_MAGIC):
        return _Container.CFB
    if data.startswith(_ZIP_MAGIC):
        return _Container.ZIP
    return None


def _cfb_root_streams(data: bytes) -> set[str] | None:
    """Names of the entries directly under the root of a compound file.

    A minimal reader rather than a dependency: just the header, the FAT chain
    and the directory, which is all it takes to tell Word from Excel. Returns
    None for anything malformed or truncated, and never reads outside ``data``.
    Only the root's own children count: an Excel chart embedded in a PowerPoint
    deck carries a `Workbook` stream too, deeper down, and must not win.
    """
    try:
        shift = struct.unpack_from("<H", data, 30)[0]
        if shift not in (9, 12):
            return None
        size = 1 << shift
        fat_count = struct.unpack_from("<I", data, 44)[0]
        dir_start = struct.unpack_from("<I", data, 48)[0]
        difat_start = struct.unpack_from("<I", data, 68)[0]
        difat_count = struct.unpack_from("<I", data, 72)[0]
        total = len(data) // size

        def sector(index: int) -> bytes:
            start = (index + 1) * size
            chunk = data[start : start + size]
            if len(chunk) != size:
                raise ValueError("sector outside the file")
            return chunk

        fat_sectors = list(struct.unpack_from("<109I", data, 76))
        next_difat = difat_start
        for _ in range(min(difat_count, total)):
            block = struct.unpack(f"<{size // 4}I", sector(next_difat))
            fat_sectors.extend(block[:-1])
            next_difat = block[-1]
        fat: list[int] = []
        for index in fat_sectors[: min(fat_count, total)]:
            fat.extend(struct.unpack(f"<{size // 4}I", sector(index)))

        directory = b""
        index, hops = dir_start, 0
        while index < 0xFFFFFFFA and hops <= total:
            directory += sector(index)
            index = fat[index]
            hops += 1
    except (struct.error, ValueError, IndexError):
        return None

    def entry(number: int) -> tuple[str, int, int, int, int] | None:
        offset = number * 128
        if offset + 128 > len(directory):
            return None
        length = struct.unpack_from("<H", directory, offset + 64)[0]
        name = directory[offset : offset + max(length - 2, 0)].decode("utf-16-le", "replace")
        left, right, child = struct.unpack_from("<III", directory, offset + 68)
        return name, directory[offset + 66], left, right, child

    root = entry(0)
    if root is None or root[1] != 5:
        return None
    names: set[str] = set()
    pending, seen = [root[4]], set[int]()
    while pending:
        number = pending.pop()
        if number >= 0xFFFFFFFA or number in seen:
            continue
        seen.add(number)
        found = entry(number)
        if found is None:
            continue
        names.add(found[0])
        pending.extend((found[2], found[3]))
    return names


def _legacy_kind(data: bytes) -> _Legacy:
    names = _cfb_root_streams(data) or set()
    if "EncryptedPackage" in names:
        return _Legacy.ENCRYPTED
    if "WordDocument" in names:
        return _Legacy.WORD
    if names & {"Workbook", "Book"}:
        return _Legacy.EXCEL
    if "PowerPoint Document" in names:
        return _Legacy.POWERPOINT
    return _Legacy.OTHER


def _ooxml_type(data: bytes) -> str | None:
    """Which OOXML format a zip is, from its parts, or None if none of them."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return _DOCX
    if "xl/workbook.xml" in names:
        return _XLSX
    if "ppt/presentation.xml" in names:
        return _PPTX
    return None


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


def _antiword(data: bytes) -> ExtractionResponse:
    """Text from a legacy Word document, via the antiword binary.

    A subprocess with no shell, an argument list, a wall-clock limit and a
    scratch directory that is removed afterwards. antiword wants a seekable
    file, and OLE2 cannot be streamed through a pipe. `-w 0` turns off its
    80-column wrapping, which would otherwise break a name or an IBAN across
    two lines and past the detector.
    """
    binary = shutil.which("antiword")
    if binary is None:
        return ExtractionResponse(kind=ExtractionKind.UNSUPPORTED, detail=ANTIWORD_MISSING)
    with tempfile.TemporaryDirectory(prefix="extract-") as scratch:
        path = os.path.join(scratch, "document.doc")
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell, our own temp path
                [binary, "-w", "0", "-m", _ANTIWORD_MAPPING, path],
                capture_output=True,
                timeout=ANTIWORD_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("antiword timed out after %ss", ANTIWORD_TIMEOUT_SECONDS)
            return ExtractionResponse(
                kind=ExtractionKind.UNREADABLE,
                detail=f"reading the old Word .doc took longer than {ANTIWORD_TIMEOUT_SECONDS}s",
            )
        except OSError as exc:
            logger.warning("could not run antiword: %s", type(exc).__name__)
            return ExtractionResponse(kind=ExtractionKind.UNSUPPORTED, detail=ANTIWORD_MISSING)
    if done.returncode != 0:
        # antiword's stderr is its own fixed vocabulary, not document content,
        # but only the cases a caller can act on are passed on.
        stderr = done.stderr.decode("utf-8", "replace").lower()
        logger.warning("antiword exited %s", done.returncode)
        if "encrypted" in stderr:
            detail = "this old Word .doc is password-protected; remove the password and resend"
        elif "mapping file" in stderr:
            detail = "the extractor image is missing antiword's UTF-8 mapping file"
        else:
            detail = (
                "the old Word .doc could not be read; it may be corrupt or from a very old Word"
            )
        return ExtractionResponse(kind=ExtractionKind.UNREADABLE, detail=detail)
    return _clip(done.stdout.decode("utf-8", "replace"), "antiword")


def _refuse_legacy(kind: _Legacy) -> ExtractionResponse:
    reasons = {
        _Legacy.POWERPOINT: "old PowerPoint .ppt (pre-2007) is not supported; save as .pptx",
        _Legacy.ENCRYPTED: "this Office file is password-protected; remove the password and resend",
        _Legacy.OTHER: (
            "this is an old Office/OLE file that is not a Word, Excel or PowerPoint "
            "document the extractor can read"
        ),
    }
    return ExtractionResponse(kind=ExtractionKind.UNSUPPORTED, detail=reasons[kind])


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

    declared = media_type_for(media_type, filename)
    container = _container(data)

    if container is _Container.CFB:
        # Whatever the client called it: a `.docx` that is really a Word 97
        # file lands here, and so does an `.xls` saved as `.xlsx`.
        legacy = _legacy_kind(data)
        if legacy is _Legacy.WORD:
            return _antiword(data)
        if legacy is not _Legacy.EXCEL:
            return _refuse_legacy(legacy)
        resolved: str | None = _XLS
    elif container is _Container.ZIP:
        # A zip is OOXML, and its parts say which. The declared type only
        # matters if the parts do not say.
        resolved = _ooxml_type(data) or (declared if declared in _ZIP_BACKED else None)
        if resolved is None:
            return ExtractionResponse(
                kind=ExtractionKind.UNSUPPORTED,
                detail="this is a ZIP archive, not a Word, Excel or PowerPoint document",
            )
    else:
        resolved = declared
        if resolved is None:
            return ExtractionResponse(
                kind=ExtractionKind.UNSUPPORTED,
                detail=f"no extractor for {media_type or 'an unnamed type'}",
            )
        if resolved in _ZIP_BACKED or resolved in {_DOC, _XLS}:
            names = {_DOC: "Word .doc", _XLS: "Excel .xls"}
            return ExtractionResponse(
                kind=ExtractionKind.UNREADABLE,
                detail=(
                    f"the file is declared as {names.get(resolved, _ACCEPTED[resolved])} "
                    "but its contents are not an Office document (corrupt, or a different format)"
                ),
            )

    assert resolved is not None  # every branch above either set it or returned
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
