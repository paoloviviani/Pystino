"""Writers for the pre-2007 Office container the extractor's tests need.

Nothing on the CI host can author a legacy `.doc`: python-docx writes OOXML,
and LibreOffice is not installed. So this builds the smallest files that are
genuinely in the old format — an OLE2 / Compound File Binary container holding
a Word 6/95 `WordDocument` stream — rather than committing an opaque blob with
no provenance, or a copyrighted one (antiword's own sample is GPL).

Honest scope: these are produced by this script, not by Word. They are valid
enough for antiword to read, which is what the tests need, and `python
legacy_office.py` regenerates ``fixtures/legacy-it.doc`` byte for byte.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Final

SECTOR: Final = 512
ENDOFCHAIN: Final = 0xFFFFFFFE
FATSECT: Final = 0xFFFFFFFD
FREESECT: Final = 0xFFFFFFFF
NOSTREAM: Final = 0xFFFFFFFF

#: A stream under this size lives in the mini stream, which this writer does not
#: implement. Every stream is padded to it instead.
MIN_STREAM: Final = 4096

CFB_MAGIC: Final = bytes.fromhex("d0cf11e0a1b11ae1")


def _directory_entry(
    name: str, kind: int, start: int, size: int, *, sibling: int = NOSTREAM, child: int = NOSTREAM
) -> bytes:
    raw = name.encode("utf-16-le") + b"\x00\x00"
    return (
        raw.ljust(64, b"\x00")
        + struct.pack("<H", len(raw))
        + struct.pack("<BB", kind, 1)  # object type, colour: black
        + struct.pack("<III", NOSTREAM, sibling, child)  # left, right, child
        + b"\x00" * 16  # CLSID
        + struct.pack("<I", 0)  # state bits
        + b"\x00" * 16  # created, modified
        + struct.pack("<I", start)
        + struct.pack("<Q", size)
    )


def build_cfb(streams: dict[str, bytes]) -> bytes:
    """A version-3 compound file with the given top-level streams.

    One FAT sector, so up to 128 sectors (64 KiB) in total: ample for fixtures.
    """
    entries = [("Root Entry", 5, ENDOFCHAIN, 0)]
    body: list[bytes] = []
    next_sector = 2  # 0 is the FAT, 1 the directory
    for name, content in streams.items():
        padded = content.ljust(max(MIN_STREAM, -(-len(content) // SECTOR) * SECTOR), b"\x00")
        count = len(padded) // SECTOR
        entries.append((name, 2, next_sector, len(padded)))
        body.append(padded)
        next_sector += count
    assert next_sector <= 128, "fixture too large for a single FAT sector"

    fat = [FATSECT, ENDOFCHAIN]  # the FAT itself, then a one-sector directory
    for _, _kind, start, size in entries[1:]:
        count = size // SECTOR
        fat.extend(range(start + 1, start + count))
        fat.append(ENDOFCHAIN)
    fat_sector = struct.pack("<128I", *(fat + [FREESECT] * (128 - len(fat))))

    # Siblings chain the streams as a right-leaning tree, which is a valid if
    # unbalanced red-black tree for a handful of nodes only because every node
    # is coloured black and the reader never rebalances.
    directory = _directory_entry("Root Entry", 5, ENDOFCHAIN, 0, child=1)
    for index, (name, kind, start, size) in enumerate(entries[1:], start=1):
        sibling = index + 1 if index + 1 < len(entries) else NOSTREAM
        directory += _directory_entry(name, kind, start, size, sibling=sibling)
    directory = directory.ljust(SECTOR, b"\x00")

    header = (
        CFB_MAGIC
        + b"\x00" * 16  # CLSID
        + struct.pack("<HHH", 0x003E, 0x0003, 0xFFFE)  # minor, major, little-endian
        + struct.pack("<HH", 9, 6)  # sector shift 512, mini sector shift 64
        + b"\x00" * 6
        + struct.pack("<III", 0, 1, 1)  # dir sectors (v3: 0), FAT sectors, dir start
        + struct.pack("<II", 0, 4096)  # transaction signature, mini cutoff
        + struct.pack("<II", ENDOFCHAIN, 0)  # mini FAT start, count
        + struct.pack("<II", ENDOFCHAIN, 0)  # DIFAT start, count
        + struct.pack("<109I", 0, *([FREESECT] * 108))  # DIFAT: the one FAT sector
    )
    return header + fat_sector + directory + b"".join(body)


def word6_stream(text: str) -> bytes:
    """A Word 6/95 `WordDocument` stream whose body is ``text``.

    Only the FIB fields antiword consults for an unsaved-fast, unencrypted
    document: identifier, version, where the text starts and how long it is.
    Text is Windows-1252, as Word 6 stored it; paragraphs end in CR.
    """
    body = text.replace("\n", "\r").encode("cp1252")
    text_start = 0x200
    fib = bytearray(text_start)
    struct.pack_into("<HH", fib, 0x00, 0xA5EC, 101)  # wIdent, nFib: Word 6 for Windows
    struct.pack_into("<I", fib, 0x18, text_start)  # fcMin
    struct.pack_into("<I", fib, 0x1C, text_start + len(body))  # fcMac
    struct.pack_into("<I", fib, 0x34, len(body))  # ccpText
    # A stylesheet with one empty style. Without it antiword reads its count
    # from a zero-length buffer, which is uninitialised memory: the file then
    # crashes the reader at random, which no fixture should do.
    stylesheet = struct.pack("<H", 18) + struct.pack("<HHHHHHHHH", 1, 10, 0, 0, 0, 0, 0, 0, 0)
    stylesheet += struct.pack("<H", 0)
    struct.pack_into("<II", fib, 0x60, text_start + len(body), len(stylesheet))  # fcStshf, lcbStshf
    return bytes(fib) + body + stylesheet


def doc_bytes(text: str) -> bytes:
    return build_cfb({"WordDocument": word6_stream(text)})


def _record(kind: int, payload: bytes) -> bytes:
    return struct.pack("<HH", kind, len(payload)) + payload


def xls_bytes(rows: list[list[str]]) -> bytes:
    """A BIFF8 workbook of text cells in a CFB `Workbook` stream.

    Hand-assembled for the same reason as the Word file; xlrd, which markitdown
    uses for `.xls`, reads it.
    """

    def bof(kind: int) -> bytes:
        return _record(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0x0DBB, 0x07CC, 0, 0x06))

    eof = _record(0x000A, b"")
    name = b"Foglio1"

    def boundsheet(offset: int) -> bytes:
        return _record(0x0085, struct.pack("<IBBB", offset, 0, 0, len(name)) + b"\x00" + name)

    sheet = bof(0x10) + _record(0x0200, struct.pack("<IIHHH", 0, len(rows), 0, 2, 0))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            raw = value.encode("cp1252")
            sheet += _record(0x0204, struct.pack("<HHHHB", r, c, 0, len(raw), 0) + raw)
    sheet += eof
    head = len(bof(5)) + len(boundsheet(0)) + len(eof)
    return build_cfb({"Workbook": bof(5) + boundsheet(head) + eof + sheet})


def ppt_bytes() -> bytes:
    return build_cfb({"PowerPoint Document": b"\x00" * 32})


#: What the committed fixture says. Italian, because the case that was reported
#: was an Italian menu and accents are what a wrong encoding destroys first.
FIXTURE_TEXT: Final = (
    "Menù autunno inverno\n"
    "Perché è già così: tè, caffè, città, più, virtù.\n"
    "àèéìòù ÀÈÉÌÒÙ\n"
    "Contatto: Luca Bianchi, IBAN IT60X0542811101000000123456\n"
)

FIXTURE: Final = Path(__file__).parent / "fixtures" / "legacy-it.doc"

if __name__ == "__main__":
    FIXTURE.write_bytes(doc_bytes(FIXTURE_TEXT))
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
