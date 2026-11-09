"""Extractor settings: which engine reads documents, and within what limits.

Read from the environment on every call rather than once at import, because a
test (and a future per-request rule) has to be able to change them, and because
the values are cheap. Nothing here imports an optional library: whether Docling
or RapidOCR is *installed* is answered where they are loaded, so the default
image never pays for them.

``EXTRACTOR_ENGINE``
    ``markitdown`` (the default) or ``docling``. A request may name the other
    one with the ``X-Extractor-Engine`` header; an unknown name is ignored and
    the configured engine answers, since a header must never be a way to make
    the service fail.
``EXTRACTOR_OCR``
    ``none`` (the default, except in the `ocr` and `docling` image variants) or
    ``rapidocr``: read pages with no text layer, and image files, with RapidOCR.
    The Docling engine always uses RapidOCR.
``EXTRACTOR_THREADS``
    Intra-op threads for ONNX and torch. Defaults to the cores this process may
    use (the cgroup-aware affinity mask, not the host's count).
``EXTRACTOR_MODELS_DIR``
    Where the image keeps the baked weights. Nothing is ever downloaded at
    runtime: a missing weight is reported as the engine being unavailable.
``EXTRACTOR_MAX_PAGES``
    Per-document page cap for OCR and Docling, so a 900-page scan cannot hold
    the process for an hour. Pages past the cap are not read and the response
    says `truncated`.
``EXTRACTOR_TIMEOUT_SECONDS``
    A soft wall-clock budget per document, checked between pages. A page being
    read is not interrupted (a thread cannot be killed safely), so the real
    bound is this plus one page.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

DEFAULT_MODELS_DIR: Final = "/opt/extractor-models"
DEFAULT_MAX_PAGES: Final = 200
DEFAULT_TIMEOUT_SECONDS: Final = 300
ENGINE_HEADER: Final = "x-extractor-engine"


class Engine(StrEnum):
    MARKITDOWN = "markitdown"
    DOCLING = "docling"


def parse_engine(value: str | None) -> Engine | None:
    """The engine a string names, or None for anything else (including empty)."""
    try:
        return Engine((value or "").strip().lower())
    except ValueError:
        return None


def _variant_default_ocr() -> str:
    """OCR is on by default in the `ocr` and `docling` image variants (the
    Dockerfile's EXTRACTOR build argument, recorded as EXTRACTOR_VARIANT)."""
    variant = os.environ.get("EXTRACTOR_VARIANT", "none").strip().lower()
    return "rapidocr" if variant in ("ocr", "docling") else "none"


def cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # not Linux
        return max(1, os.cpu_count() or 1)


def _int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    engine: Engine
    ocr: bool
    threads: int
    models_dir: Path
    max_pages: int
    timeout_seconds: int


def settings() -> Settings:
    return Settings(
        engine=parse_engine(os.environ.get("EXTRACTOR_ENGINE")) or Engine.MARKITDOWN,
        ocr=(os.environ.get("EXTRACTOR_OCR") or _variant_default_ocr()).strip().lower()
        == "rapidocr",
        threads=_int("EXTRACTOR_THREADS", cpu_count()),
        models_dir=Path(os.environ.get("EXTRACTOR_MODELS_DIR") or DEFAULT_MODELS_DIR),
        max_pages=_int("EXTRACTOR_MAX_PAGES", DEFAULT_MAX_PAGES),
        timeout_seconds=_int("EXTRACTOR_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
    )


def available_engines(config: Settings | None = None) -> list[str]:
    """The readers this process can run now, in the order the gateway lists them.

    ``markitdown`` always. ``ocr`` (markitdown with OCR switched on for one
    request) needs RapidOCR and its weights; ``docling`` needs Docling, its
    weights and RapidOCR. Answered from the same checks the readers use, so the
    list cannot promise an engine that would answer "unavailable" and fall back.
    The imports are here, not at the top: the default image has neither library.
    """
    from llmp_redaction import docling_engine, ocr

    config = config or settings()
    found = [Engine.MARKITDOWN.value]
    if ocr.available(config):
        found.append("ocr")
    if docling_engine.available(config):
        found.append(Engine.DOCLING.value)
    return found
