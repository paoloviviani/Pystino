"""Engine selection, fallback per format, OCR on scans, and the limits.

Docling and RapidOCR are replaced by fakes here: these tests are about which
reader gets which document and what happens when it fails, and must run in an
environment that has neither (the default image, CI). What the real engines
read is in test_docling_integration.py, gated on the weights being present.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from llmp_shared import ExtractionKind

pytest.importorskip("markitdown", reason="needs services/redaction's own environment")
pytest.importorskip("reportlab", reason="the synthetic PDFs are authored with reportlab")
import synth_pdf
from fastapi.testclient import TestClient
from llmp_redaction import docling_engine, documents, ocr
from llmp_redaction.app import create_app
from llmp_redaction.docling_engine import Converted, DoclingUnavailable
from llmp_redaction.documents import extract
from llmp_redaction.engines import Engine, cpu_count, parse_engine, settings
from test_documents import DOCX, PPTX, XLSX, docx_bytes, xlsx_bytes

PDF = "application/pdf"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "EXTRACTOR_ENGINE",
        "EXTRACTOR_OCR",
        "EXTRACTOR_THREADS",
        "EXTRACTOR_MAX_PAGES",
        "EXTRACTOR_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeDocling:
    """Stands in for `docling_engine.convert`, recording what it was asked."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, text: str = "# From docling\n\n| a |\n"
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.text = text
        self.error: Exception | None = None
        monkeypatch.setattr(docling_engine, "convert", self)

    def __call__(self, data: bytes, **kwargs: object) -> Converted:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return Converted(text=self.text, pages=2, page_texts=[self.text, ""], partial=False)


def test_defaults_are_markitdown_without_ocr() -> None:
    config = settings()
    assert config.engine is Engine.MARKITDOWN
    assert config.ocr is False
    assert config.threads == cpu_count()


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "Docling")
    monkeypatch.setenv("EXTRACTOR_OCR", "rapidocr")
    monkeypatch.setenv("EXTRACTOR_THREADS", "1")
    monkeypatch.setenv("EXTRACTOR_MAX_PAGES", "7")
    config = settings()
    assert (config.engine, config.ocr, config.threads, config.max_pages) == (
        Engine.DOCLING,
        True,
        1,
        7,
    )
    monkeypatch.setenv("EXTRACTOR_THREADS", "many")
    assert settings().threads == cpu_count()


def test_an_unknown_engine_name_is_not_an_engine() -> None:
    assert parse_engine("docling") is Engine.DOCLING
    assert parse_engine("tesseract") is None
    assert parse_engine("") is None
    assert parse_engine(None) is None


def test_markitdown_is_the_default_and_docling_is_not_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDocling(monkeypatch)
    result = extract(docx_bytes("Plain paragraph"), media_type=DOCX)
    assert result.extractor == "markitdown"
    assert fake.calls == []


@pytest.mark.parametrize("media_type", [DOCX, XLSX])
def test_the_docling_engine_reads_office_formats(
    monkeypatch: pytest.MonkeyPatch, media_type: str
) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    fake = FakeDocling(monkeypatch)
    data = docx_bytes("x") if media_type == DOCX else xlsx_bytes([["a", "b"]])
    result = extract(data, media_type=media_type)
    assert result.kind is ExtractionKind.TEXT
    assert result.extractor == "docling"
    assert "From docling" in result.text
    assert len(fake.calls) == 1


def test_a_request_can_pick_the_engine_over_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDocling(monkeypatch)
    assert extract(docx_bytes("x"), media_type=DOCX, engine="docling").extractor == "docling"
    assert len(fake.calls) == 1
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    assert extract(docx_bytes("x"), media_type=DOCX, engine="markitdown").extractor == "markitdown"
    assert len(fake.calls) == 1
    # a name that is not an engine means the configured engine, never an error
    assert extract(docx_bytes("x"), media_type=DOCX, engine="nonsense").extractor == "docling"


def test_the_ocr_reader_without_the_weights_says_so_and_reads_as_markitdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routing rule asking for `ocr` on a build without OCR must be told, not
    quietly given a text-layer read (ADR 0113)."""
    monkeypatch.setattr(ocr, "available", lambda config=None: False)
    result = extract(docx_bytes("Plain paragraph"), media_type=DOCX, engine="ocr")
    assert result.kind is ExtractionKind.TEXT
    assert result.extractor == "markitdown"
    assert result.fallback_reason == "ocr unavailable"


def test_the_ocr_reader_switches_ocr_on_for_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llmp_shared import ExtractionResponse

    seen: list[bool] = []

    def by_page(data: bytes, *, max_pages: int, long_side: int, config: object) -> object:
        seen.append(config.ocr)  # type: ignore[attr-defined]
        return ExtractionResponse(kind=ExtractionKind.TEXT, text="read", extractor="rapidocr")

    monkeypatch.setattr(ocr, "available", lambda config=None: True)
    monkeypatch.setattr(documents, "_pdf_by_page", by_page)
    pdf = _blank_pdf(200)
    # EXTRACTOR_OCR is unset, so a plain request does not use the OCR path...
    extract(pdf, media_type=PDF)
    assert seen == []
    # ...and a request that asks for the `ocr` reader does, once.
    result = extract(pdf, media_type=PDF, engine="ocr")
    assert seen == [True] and result.extractor == "rapidocr" and result.fallback_reason == ""


@pytest.mark.parametrize(
    "failure",
    [DoclingUnavailable("no weights"), RuntimeError("boom"), ValueError("bad page")],
)
def test_docling_failing_falls_back_to_markitdown(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    fake = FakeDocling(monkeypatch)
    fake.error = failure
    result = extract(docx_bytes("Fallback paragraph"), media_type=DOCX)
    assert result.extractor == "markitdown"
    assert "Fallback paragraph" in result.text


def test_an_empty_docling_result_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    FakeDocling(monkeypatch, text="   ")
    result = extract(docx_bytes("Only markitdown has this"), media_type=DOCX)
    assert result.extractor == "markitdown"


def test_legacy_formats_never_go_to_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    from legacy_office import FIXTURE, xls_bytes

    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    fake = FakeDocling(monkeypatch)
    old_excel = extract(xls_bytes([["a", "b"]]), media_type="application/vnd.ms-excel")
    assert old_excel.extractor == "markitdown"
    import shutil

    if shutil.which("antiword"):
        old_word = extract(Path(FIXTURE).read_bytes(), media_type="application/msword")
        assert old_word.extractor == "antiword"
    assert fake.calls == []


def test_docling_pdf_pages_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    monkeypatch.setenv("EXTRACTOR_MAX_PAGES", "1")
    fake = FakeDocling(monkeypatch)
    result = extract(synth_pdf.report_pdf(scan_pages=0), media_type=PDF)
    assert result.extractor == "docling"
    assert result.pages == 1
    assert fake.calls[0]["page_limit"] is None  # one page, cap of one: nothing cut
    result = extract(synth_pdf.report_pdf(scan_pages=2), media_type=PDF)
    assert fake.calls[1]["page_limit"] == 1
    assert result.truncated is True
    assert result.pages == 3
    assert PPTX in documents._DOCLING_TYPES


def test_docling_pdf_with_page_images_keeps_the_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    FakeDocling(monkeypatch)
    result = extract(synth_pdf.report_pdf(scan_pages=1), media_type=PDF, page_images=3)
    assert result.extractor == "docling"
    assert result.pages == 2
    assert result.page_texts[1] == ""
    # the page docling could not read is still offered as a picture
    assert [image.page for image in result.page_images] == [2]
    assert result.inspected is False


# --- OCR on the markitdown path -------------------------------------------------


@pytest.fixture
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    monkeypatch.setenv("EXTRACTOR_OCR", "rapidocr")
    seen: list[int] = []
    monkeypatch.setattr(ocr, "available", lambda config=None: True)

    def read(image: object, config: object = None) -> str:
        seen.append(1)
        return "Testo letto dal OCR"

    monkeypatch.setattr(ocr, "read_image", read)
    return seen


def test_a_scanned_page_is_read_when_ocr_is_on(fake_ocr: list[int]) -> None:
    result = extract(synth_pdf.report_pdf(scan_pages=1), media_type=PDF)
    assert result.kind is ExtractionKind.TEXT
    assert result.extractor == "pypdfium2+rapidocr"
    assert "Testo letto dal OCR" in result.text
    assert "REFERTO" in result.text  # the text page keeps its own layer
    assert result.page_images == []
    assert result.inspected is True
    assert len(fake_ocr) == 1


def test_ocr_stops_at_the_page_cap(fake_ocr: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_MAX_PAGES", "1")
    result = extract(synth_pdf.report_pdf(scan_pages=3), media_type=PDF, page_images=5)
    assert len(fake_ocr) == 1
    assert result.truncated is True
    assert "not read" in result.detail
    assert [image.page for image in result.page_images] == [3, 4]
    assert result.inspected is False


def test_a_page_ocr_cannot_read_stays_a_picture(
    fake_ocr: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "read_image", lambda image, config=None: "")
    result = extract(synth_pdf.report_pdf(scan_pages=1), media_type=PDF, page_images=2)
    assert [image.page for image in result.page_images] == [2]
    assert result.inspected is False


def test_text_only_pdfs_are_unchanged_by_ocr(fake_ocr: list[int]) -> None:
    result = extract(synth_pdf.report_pdf(scan_pages=0), media_type=PDF)
    assert result.extractor == "markitdown"
    assert fake_ocr == []


def test_a_picture_file_is_read_with_ocr(fake_ocr: list[int]) -> None:
    result = extract(synth_pdf.png_bytes(), media_type="image/png")
    assert result.kind is ExtractionKind.TEXT
    assert result.extractor == "rapidocr"


def test_a_picture_file_is_unsupported_without_ocr() -> None:
    result = extract(synth_pdf.png_bytes(), media_type="image/png")
    assert result.kind is ExtractionKind.UNSUPPORTED
    assert "image/png" in result.detail


def test_an_oversized_picture_is_refused(
    fake_ocr: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents, "MAX_IMAGE_PIXELS", 100)
    result = extract(synth_pdf.png_bytes(), media_type="image/png")
    assert result.kind is ExtractionKind.UNREADABLE
    assert fake_ocr == []


def test_a_corrupt_picture_is_reported_not_raised(fake_ocr: list[int]) -> None:
    result = extract(b"\x89PNG\r\n\x1a\nnot really", media_type="image/png")
    assert result.kind is ExtractionKind.UNREADABLE


def test_ocr_reports_unavailable_without_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXTRACTOR_MODELS_DIR", str(tmp_path))
    assert ocr.available() is False
    with pytest.raises(ocr.OcrUnavailable):
        ocr.read_image(__import__("PIL.Image").Image.new("RGB", (8, 8)))


# --- the endpoint -----------------------------------------------------------------


def test_the_engine_header_reaches_the_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
    fake = FakeDocling(monkeypatch)
    with TestClient(create_app()) as client:
        body = io.BytesIO(docx_bytes("x")).getvalue()
        default = client.post("/extract", content=body, headers={"content-type": DOCX}).json()
        picked = client.post(
            "/extract",
            content=body,
            headers={"content-type": DOCX, "x-extractor-engine": "docling"},
        ).json()
        junk = client.post(
            "/extract",
            content=body,
            headers={"content-type": DOCX, "x-extractor-engine": "bogus"},
        ).json()
    assert (default["extractor"], picked["extractor"], junk["extractor"]) == (
        "markitdown",
        "docling",
        "markitdown",
    )
    assert len(fake.calls) == 1


# --- review follow-ups: caps, reasons, budget ------------------------------------


def _blank_pdf(side: float) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(side, side))
    page.showPage()
    page.save()
    return buffer.getvalue()


def test_a_giant_page_is_rendered_within_the_ocr_cap(
    fake_ocr: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[tuple[int, int]] = []

    def read(image: object, config: object = None) -> str:
        sizes.append(image.size)  # type: ignore[attr-defined]
        return "pagina enorme"

    monkeypatch.setattr(ocr, "read_image", read)
    # 14400 points is 200 inches: at a fixed 200 DPI that is a 40,000 pixel square.
    result = extract(_blank_pdf(14400), media_type=PDF)
    assert result.extractor == "pypdfium2+rapidocr"
    assert len(sizes) == 1
    assert max(sizes[0]) <= ocr.MAX_SIDE


def test_an_ordinary_page_still_gets_200_dpi() -> None:
    assert documents._ocr_scale(595, 842) == pytest.approx(200 / 72)
    assert documents._ocr_scale(14400, 14400) * 14400 <= ocr.MAX_SIDE + 1


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (DoclingUnavailable("x"), "docling unavailable"),
        (docling_engine.DoclingBusy(), "docling busy"),
        (docling_engine.DoclingTimeout(), "docling timeout"),
        (RuntimeError("boom"), "docling failed"),
    ],
)
def test_a_fallback_says_why_without_content(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, reason: str
) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    fake = FakeDocling(monkeypatch)
    fake.error = failure
    result = extract(docx_bytes("Plain"), media_type=DOCX)
    assert result.extractor == "markitdown"
    assert result.fallback_reason == reason


def test_no_fallback_reason_when_the_engine_asked_for_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    FakeDocling(monkeypatch)
    assert extract(docx_bytes("x"), media_type=DOCX).fallback_reason == ""
    monkeypatch.setenv("EXTRACTOR_ENGINE", "markitdown")
    assert extract(docx_bytes("x"), media_type=DOCX).fallback_reason == ""


def test_an_empty_docling_result_is_a_reason_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    FakeDocling(monkeypatch, text=" ")
    result = extract(docx_bytes("x"), media_type=DOCX)
    assert result.fallback_reason == "docling returned no text"


def test_the_fallback_shares_the_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    monkeypatch.setenv("EXTRACTOR_OCR", "rapidocr")
    monkeypatch.setenv("EXTRACTOR_TIMEOUT_SECONDS", "840")
    monkeypatch.setattr(ocr, "available", lambda config=None: True)
    fake = FakeDocling(monkeypatch)
    fake.error = docling_engine.DoclingTimeout()
    clock = iter([1000.0, 1800.0])  # the call starts, then Docling has used 800 s
    monkeypatch.setattr(documents, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    seen: list[int] = []

    def by_page(data: bytes, **kwargs: object) -> None:
        seen.append(kwargs["config"].timeout_seconds)  # type: ignore[attr-defined]
        return None

    monkeypatch.setattr(documents, "_pdf_by_page", by_page)
    result = extract(synth_pdf.report_pdf(scan_pages=0), media_type=PDF)
    assert seen == [40]
    assert result.fallback_reason == "docling timeout"


def test_pages_are_counted_by_pdfium() -> None:
    assert documents._pdf_pages(synth_pdf.report_pdf(scan_pages=2)) == 3
    assert documents._pdf_pages(b"not a pdf") == 0


def test_the_page_cap_applies_when_pages_cannot_be_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTRACTOR_ENGINE", "docling")
    monkeypatch.setenv("EXTRACTOR_MAX_PAGES", "2")
    monkeypatch.setattr(documents, "_pdf_pages", lambda data: 0)
    fake = FakeDocling(monkeypatch)
    result = extract(synth_pdf.report_pdf(scan_pages=0), media_type=PDF)
    assert fake.calls[0]["page_limit"] == 2
    assert result.truncated is True  # the fake reports two pages: the cap was reached


def test_docling_busy_hands_the_document_on(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("docling", reason="convert() imports Docling before it takes the lock")
    monkeypatch.setattr(docling_engine, "LOCK_WAIT_SECONDS", 0.05)
    with docling_engine._lock, pytest.raises(docling_engine.DoclingBusy):
        docling_engine.convert(b"x", extension=".docx", config=settings())


def test_the_notice_names_every_model_and_carries_the_cdla_text(tmp_path: Path) -> None:
    import json
    import sys

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import fetch_models
    finally:
        sys.path.remove(str(scripts))
    lock = json.loads((scripts / "models.lock.json").read_text())
    fetch_models.write_notice(lock, tmp_path, docling=True)
    notice = (tmp_path / "NOTICE.md").read_text()
    for repo, entry in lock["huggingface"].items():
        assert repo in notice and entry["revision"] in notice
    assert "PP-OCRv6" in notice and "CDLA-Permissive-2.0" in notice
    assert (
        "makes available the text of this agreement"
        in (tmp_path / "CDLA-Permissive-2.0.txt").read_text()
    )
    ocr_only = tmp_path / "ocr"
    ocr_only.mkdir()
    fetch_models.write_notice(lock, ocr_only, docling=False)
    assert "CDLA" not in (ocr_only / "NOTICE.md").read_text()
    assert not (ocr_only / "CDLA-Permissive-2.0.txt").exists()


def test_healthz_reports_the_engines_this_build_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway offers one OCR model per name here, so the list must be true."""
    monkeypatch.setenv("REDACTION_NLP_ENGINE", "disabled")
    with TestClient(create_app()) as client:
        # No weights, no libraries: markitdown only.
        monkeypatch.setattr(ocr, "available", lambda config=None: False)
        monkeypatch.setattr(docling_engine, "available", lambda config: False)
        assert client.get("/healthz").json()["extractor_engines"] == ["markitdown"]

        # The ocr variant: RapidOCR and its weights, no Docling.
        monkeypatch.setattr(ocr, "available", lambda config=None: True)
        assert client.get("/healthz").json()["extractor_engines"] == ["markitdown", "ocr"]

        # The docling variant.
        monkeypatch.setattr(docling_engine, "available", lambda config: True)
        assert client.get("/healthz").json()["extractor_engines"] == [
            "markitdown",
            "ocr",
            "docling",
        ]
