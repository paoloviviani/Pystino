"""The real engines on a synthetic Italian medical-style report.

Gated on the baked weights: set ``EXTRACTOR_MODELS_DIR`` to a directory made by
``scripts/fetch_models.py`` (the `-docling` and `-ocr` image variants have one at
`/opt/extractor-models`). Without them these skip, with the reason; the default
CI does not carry the weights. Run them by hand, or inside the variant image:

    EXTRACTOR_MODELS_DIR=/path/to/models uv run pytest tests/test_docling_integration.py

The document is `synth_pdf.report_pdf`: two columns of invented clinical prose,
a ruled table below them, and a second page that is only a bitmap (no text layer)
carrying a fiscal code and accented Italian.
"""

from __future__ import annotations

import pytest

pytest.importorskip("markitdown", reason="needs services/redaction's own environment")
pytest.importorskip("reportlab", reason="the synthetic PDFs are authored with reportlab")
import synth_pdf
from llmp_redaction import docling_engine, ocr
from llmp_redaction.documents import extract
from llmp_redaction.engines import settings
from llmp_shared import ExtractionKind

PDF = "application/pdf"


@pytest.fixture(autouse=True)
def engine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("EXTRACTOR_THREADS", "2")


needs_ocr = pytest.mark.skipif(
    not ocr.available(settings()),
    reason="RapidOCR or its weights are absent (EXTRACTOR_MODELS_DIR)",
)
needs_docling = pytest.mark.skipif(
    not docling_engine.available(settings()),
    reason="Docling or its weights are absent (EXTRACTOR_MODELS_DIR)",
)


@needs_ocr
def test_ocr_reads_a_scanned_page_in_italian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_OCR", "rapidocr")
    result = extract(synth_pdf.report_pdf(), media_type=PDF)
    assert result.kind is ExtractionKind.TEXT
    assert result.extractor == "pypdfium2+rapidocr"
    assert result.pages == 2
    scan = result.page_texts[1]
    assert "RSSMRA61C42L219Z" in scan
    assert "ipertensione lieve" in scan
    assert "più" in scan  # accents survive recognition
    assert "REFERTO AMBULATORIALE" in result.page_texts[0]
    assert result.inspected


@needs_ocr
def test_ocr_reads_a_picture_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_OCR", "rapidocr")
    result = extract(synth_pdf.png_bytes(), media_type="image/png")
    assert result.extractor == "rapidocr"
    assert "RSSMRA61C42L219Z" in result.text


@needs_docling
def test_docling_reads_columns_in_order_keeps_the_table_and_ocrs_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    result = extract(synth_pdf.report_pdf(), media_type=PDF, page_images=2)
    assert result.kind is ExtractionKind.TEXT
    assert result.extractor == "docling"
    assert result.pages == 2
    text = result.text

    # Reading order: the whole left column, then the whole right column.
    left_end = text.index("murmure vescicolare")
    right_start = text.index("Conclusioni cliniche")
    assert text.index("REFERTO AMBULATORIALE") < text.index("Anamnesi") < left_end
    assert left_end < right_start < text.index("Firmato: dottor Luca Bianchi")

    # The table survives as a Markdown table, header row and a data row.
    rows = [line for line in text.splitlines() if line.startswith("|")]
    assert any("Esame" in row and "Valore" in row for row in rows)
    assert any("Colesterolo" in row and "188" in row for row in rows)
    assert any(set(row) <= set("|- ") for row in rows)  # the separator line

    # The image-only page was read by OCR, so it is text and not a picture.
    assert "RSSMRA61C42L219Z" in text
    assert "più" in text
    assert result.page_images == []
    assert result.inspected


@needs_docling
def test_docling_reads_a_word_table(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_documents import DOCX, docx_bytes

    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    result = extract(
        docx_bytes("Referto", table=[["Esame", "Valore"], ["Glicemia", "92"]]), media_type=DOCX
    )
    assert result.extractor == "docling"
    assert "| Glicemia" in result.text
