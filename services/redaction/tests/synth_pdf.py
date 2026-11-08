"""Synthetic PDFs for the OCR and Docling tests: no real document, ever.

`report_pdf` is an Italian, medical-style, two-column report with a table; page 2
is image-only (the text is rendered into a bitmap, so there is no text layer
and only OCR can read it). Authored with reportlab (BSD-3-Clause) and Pillow;
the people, numbers and codes are invented.
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image, ImageDraw, ImageFont

#: Left column, then right column, in the order a reader reads them.
LEFT: Final = [
    "REFERTO AMBULATORIALE",
    "Paziente: Maria Rossi, nata a Torino il 02/03/1961. Giunge in ambulatorio per "
    "il controllo programmato della pressione arteriosa, riferendo di stare bene.",
    "Anamnesi: ipertensione arteriosa in terapia da dieci anni, nessuna allergia "
    "nota ai farmaci, non fuma e cammina ogni giorno per mezz'ora.",
    "Esame obiettivo: pressione arteriosa 135 su 85, frequenza cardiaca regolare, "
    "toni cardiaci validi, murmure vescicolare normale su tutto l'ambito.",
]
RIGHT: Final = [
    "Conclusioni cliniche",
    "Si consiglia controllo ambulatoriale a distanza di tre mesi, con ripetizione "
    "degli esami del sangue riportati nella tabella sottostante.",
    "Proseguire la terapia antipertensiva in corso senza modifiche di dosaggio, "
    "misurando la pressione a domicilio due volte alla settimana.",
    "Firmato: dottor Luca Bianchi, medico di medicina generale.",
]
TABLE: Final = [
    ["Esame", "Valore", "Unita"],
    ["Glicemia", "92", "mg/dL"],
    ["Colesterolo", "188", "mg/dL"],
    ["Creatinina", "0,9", "mg/dL"],
]
SCAN_LINES: Final = [
    "Pagina scansionata",
    "Codice fiscale RSSMRA61C42L219Z",
    "Diagnosi: ipertensione lieve, non c'è più dolore",
]


def _font_path() -> str:
    import reportlab

    return str(reportlab.__path__[0]) + "/fonts/Vera.ttf"


def scan_image(lines: list[str] = SCAN_LINES, size: tuple[int, int] = (1240, 600)) -> Image.Image:
    """Text rendered to a bitmap, the way a scanner would leave it."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(_font_path(), 44)
    y = 60
    for line in lines:
        draw.text((60, y), line, fill="black", font=font)
        y += 90
    return image


def png_bytes(lines: list[str] = SCAN_LINES) -> bytes:
    buffer = io.BytesIO()
    scan_image(lines).save(buffer, format="PNG")
    return buffer.getvalue()


def report_pdf(*, scan_pages: int = 1) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(TTFont("Vera", _font_path()))
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    column = (width - 140) / 2
    page.setFont("Vera", 11)
    for x, lines in ((50, LEFT), (90 + column, RIGHT)):
        y = height - 80
        for line in lines:
            # wrap by hand: reportlab's canvas does not
            words, current = line.split(), ""
            for word in words:
                trial = f"{current} {word}".strip()
                if pdfmetrics.stringWidth(trial, "Vera", 11) > column:
                    page.drawString(x, y, current)
                    y -= 16
                    current = word
                else:
                    current = trial
            page.drawString(x, y, current)
            y -= 20
    # A ruled table below both columns.
    top, row_height = height - 380, 24
    col_x = [50, 200, 300, 400]
    for r, row in enumerate(TABLE):
        for c, cell in enumerate(row):
            page.drawString(col_x[c] + 6, top - r * row_height - 16, cell)
    for r in range(len(TABLE) + 1):
        page.line(col_x[0], top - r * row_height, col_x[-1], top - r * row_height)
    for x in col_x:
        page.line(x, top, x, top - len(TABLE) * row_height)
    page.showPage()
    for _ in range(scan_pages):
        page.drawImage(
            ImageReader(scan_image()),
            40,
            height - 340,
            width=width - 80,
            height=(width - 80) * 600 / 1240,
        )
        page.showPage()
    page.save()
    return buffer.getvalue()
