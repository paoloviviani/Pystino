"""The Docling engine: layout, reading order and tables, with RapidOCR for scans.

Docling's code is MIT. Its weights are read from ``EXTRACTOR_MODELS_DIR`` and
nothing is fetched at runtime (`artifacts_path` is set, remote services are
off, and plugins are not loaded). Which layout and table models those are, and
under what licence, is in docs/extractor-engines.md and in the image build.

What it reads here: PDF (text and scanned), Word, PowerPoint and Excel (OOXML).
Old `.doc`/`.xls`, and anything Docling declines or fails on, stay with the
other readers (`documents.extract` does the fallback).

One conversion at a time: the converter holds the loaded models, which are
hundreds of megabytes, and two pages of layout inference racing each other on a
four-core box are slower than one after the other and twice the memory.
"""

from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass
from typing import Any, Final

from llmp_redaction.engines import Settings

logger = logging.getLogger(__name__)

NAME: Final = "docling"
DOCLING_MISSING: Final = "The Docling engine is not available in this extractor image"

_lock = threading.Lock()
_converter: Any = None
_converter_key: tuple[object, ...] | None = None


class DoclingUnavailable(Exception):
    """Docling or its baked weights are not in this image."""


@dataclass(frozen=True)
class Converted:
    text: str
    pages: int
    #: Markdown per page, only for paged formats (PDF, PowerPoint).
    page_texts: list[str]
    #: The document timeout ran out before every page was read.
    partial: bool


def available(config: Settings) -> bool:
    folder = config.models_dir
    if not (folder / "docling-project--docling-models").is_dir():
        return False
    if not any(folder.glob("docling-project--docling-layout-*")):
        return False
    try:
        import docling  # noqa: F401
    except ImportError:
        return False
    from llmp_redaction import ocr

    return ocr.available(config)


def _build(config: Settings) -> Any:
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        artifacts_path=str(config.models_dir),
        enable_remote_services=False,
        allow_external_plugins=False,
        accelerator_options=AcceleratorOptions(
            num_threads=config.threads, device=AcceleratorDevice.CPU
        ),
        do_ocr=True,
        # One multilingual recogniser: Italian and English together.
        ocr_options=RapidOcrOptions(lang=["iso:it"], backend="onnxruntime"),
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE, do_cell_matching=True
        ),
        document_timeout=float(config.timeout_seconds),
        # No pictures are described, classified or stored: text is the product.
        generate_page_images=False,
        generate_picture_images=False,
        do_picture_classification=False,
        do_picture_description=False,
        do_code_enrichment=False,
        do_formula_enrichment=False,
    )
    return DocumentConverter(
        allowed_formats=[
            InputFormat.PDF,
            InputFormat.DOCX,
            InputFormat.PPTX,
            InputFormat.XLSX,
        ],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )


def convert(
    data: bytes, *, extension: str, config: Settings, page_limit: int | None = None
) -> Converted:
    """One document to Markdown. Raises on anything Docling cannot do.

    `page_limit` reads only the first N pages (PDF); the caller says so in the
    response. Raises `DoclingUnavailable` when the library or weights are absent.
    """
    global _converter, _converter_key
    from docling.datamodel.base_models import ConversionStatus, DocumentStream

    key = (config.models_dir, config.threads, config.timeout_seconds)
    with _lock:
        if _converter is None or key != _converter_key:
            if not available(config):
                raise DoclingUnavailable(DOCLING_MISSING)
            _converter = _build(config)
            _converter_key = key
        kwargs: dict[str, Any] = {"raises_on_error": False}
        if page_limit:
            kwargs["page_range"] = (1, page_limit)
        result = _converter.convert(
            DocumentStream(name=f"document{extension}", stream=io.BytesIO(data)), **kwargs
        )
        if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
            raise RuntimeError(f"docling status {result.status.value}")
        document = result.document
        text = document.export_to_markdown()
        pages = len(document.pages)
        per_page = (
            [document.export_to_markdown(page_no=number) for number in sorted(document.pages)]
            if extension in (".pdf", ".pptx")
            else []
        )
    return Converted(
        text=text,
        pages=pages,
        page_texts=per_page,
        partial=result.status is ConversionStatus.PARTIAL_SUCCESS,
    )
