"""Time the extractor engines on synthetic documents: pages per minute and peak memory.

One configuration per process, so the peak resident set is that configuration's
own. A document is read once to warm the models (reported separately as
`load_s`), then timed.

    EXTRACTOR_MODELS_DIR=/opt/extractor-models taskset -c 0-3 \\
        python bench_extractor.py --engine docling --doc scan --pages 5 --threads 4

Prints one JSON line. docs/extractor-engines.md has the table and how to read it.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "src"), str(HERE.parent / "tests")]

PROSE = (
    "Il paziente riferisce miglioramento dei sintomi dopo la modifica della terapia "
    "farmacologica; gli esami ematochimici di controllo, eseguiti presso il laboratorio "
    "di riferimento, mostrano valori nella norma. Si raccomanda di proseguire il "
    "trattamento e di programmare una nuova visita tra sei mesi, con ripetizione degli "
    "accertamenti strumentali gia concordati con il medico curante."
)


def text_pdf(pages: int) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    for number in range(pages):
        page.setFont("Helvetica", 10.5)
        y = A4[1] - 60
        page.drawString(50, y, f"Relazione clinica, pagina {number + 1}")
        y -= 30
        for _ in range(6):
            words, line = PROSE.split(), ""
            for word in words:
                if len(line) + len(word) > 95:
                    page.drawString(50, y, line)
                    y -= 14
                    line = word
                else:
                    line = f"{line} {word}".strip()
            page.drawString(50, y, line)
            y -= 24
        page.showPage()
    page.save()
    return buffer.getvalue()


def two_column_pdf(pages: int) -> bytes:
    import synth_pdf

    # The report page, repeated: two columns and a ruled table.
    one = synth_pdf.report_pdf(scan_pages=0)
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(one)
    out = pdfium.PdfDocument.new()
    for _ in range(pages):
        out.import_pages(document, [0])
    buffer = io.BytesIO()
    out.save(buffer)
    return buffer.getvalue()


def scan_pdf(pages: int) -> bytes:
    """A4 pages that are only a 150 DPI bitmap of dense text: no text layer."""
    import synth_pdf
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    font = ImageFont.truetype(synth_pdf._font_path(), 22)
    for number in range(pages):
        image = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(image)
        y = 100
        draw.text(
            (100, y), f"Relazione clinica scansionata, pagina {number + 1}", fill="black", font=font
        )
        y += 60
        while y < 1650:
            words, line = PROSE.split(), ""
            for word in words:
                if len(line) + len(word) > 78:
                    draw.text((100, y), line, fill="black", font=font)
                    y += 30
                    line = word
                else:
                    line = f"{line} {word}".strip()
            draw.text((100, y), line, fill="black", font=font)
            y += 50
        page.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
        page.showPage()
    page.save()
    return buffer.getvalue()


DOCS = {"text": text_pdf, "twocol": two_column_pdf, "scan": scan_pdf}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--engine", choices=("markitdown", "docling", "markitdown+ocr"), required=True
    )
    parser.add_argument("--doc", choices=sorted(DOCS), required=True)
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    os.environ["EXTRACTOR_THREADS"] = str(args.threads)
    os.environ["EXTRACTOR_ENGINE"] = "docling" if args.engine == "docling" else "markitdown"
    os.environ["EXTRACTOR_OCR"] = "rapidocr" if args.engine != "markitdown" else "none"
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ["EXTRACTOR_MAX_PAGES"] = "1000"
    os.environ["EXTRACTOR_TIMEOUT_SECONDS"] = "3600"

    import torch

    torch.set_num_threads(args.threads)
    from llmp_redaction.documents import extract

    data = DOCS[args.doc](args.pages)
    warm = DOCS[args.doc](1)
    started = time.monotonic()
    extract(warm, media_type="application/pdf")
    load_s = time.monotonic() - started
    started = time.monotonic()
    result = extract(data, media_type="application/pdf")
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {
                "engine": args.engine,
                "doc": args.doc,
                "pages": args.pages,
                "threads": args.threads,
                "seconds": round(elapsed, 2),
                "pages_per_minute": round(args.pages / elapsed * 60, 1),
                "load_s": round(load_s, 1),
                "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
                "kind": result.kind.value,
                "extractor": result.extractor,
                "chars": len(result.text),
            }
        )
    )


if __name__ == "__main__":
    main()
