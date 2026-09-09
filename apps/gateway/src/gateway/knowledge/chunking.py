"""Turning a document's text into passages worth embedding.

The extractor upstream of this returns **markdown** — that is what
``/v1/ocr`` produces, from both backends — so this splitter is markdown-aware
rather than a fixed-width slicer. Three decisions carry the quality of every
retrieval built on top, and each is here rather than in the caller because
getting them wrong is invisible until answers are subtly bad.

**Structure first, length second.** Splitting is attempted at heading
boundaries, then paragraph boundaries, then sentences, and only then mid-text.
A fixed-width slicer cuts sentences in half, and half a sentence embeds to a
point near nothing a person would ask.

**Every chunk carries its heading path.** A passage retrieved on its own is
handed to a model with no idea where it came from; "Revenue fell 4%" under
``# 2026 results > ## Italy`` means something, and alone it means nothing. The
path is prepended to the stored text, which costs tokens on both the embedding
and the eventual prompt, and buys the passage its context. This is the one
decision here that trades money for quality, deliberately.

**Overlap is measured in characters and cut at a word boundary.** A fact that
straddles a boundary is otherwise in neither chunk. Overlap is capped at a
third of the chunk size regardless of what is configured, because beyond that
the index is mostly duplicates and retrieval starts returning the same sentence
several times, which looks like a ranking bug and is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

#: An ATX markdown heading: one to six hashes, a space, then the text.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

#: A sentence end, approximately. Deliberately not a dependency: `sbd` and
#: friends bring a model or a table of abbreviations, and the failure mode here
#: is a chunk boundary two words off, not a wrong answer. What it must not do is
#: split on a decimal point or an ellipsis, which is what the lookbehind and the
#: repeated-punctuation class are for.
_SENTENCE_END = re.compile(r"(?<=[.!?])[.!?]*\s+(?=[^\s])")

#: Below this, a "chunk" is a fragment: it embeds to noise and pollutes every
#: ranking it appears in. Text shorter than this in total is still indexed as
#: one chunk — a two-line document is a legitimate document — but a *remainder*
#: this small is folded back into the previous chunk instead of standing alone.
MIN_CHUNK_CHARS = 80


@dataclass(frozen=True, slots=True)
class Chunk:
    """One passage, before it has cost anything to embed."""

    ordinal: int
    text: str
    #: The headings above it, outermost first. Kept separately as well as
    #: prepended, so a console can show provenance without re-parsing the text.
    headings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Section:
    headings: tuple[str, ...]
    body: str


def _sections(text: str) -> Iterator[_Section]:
    """Split markdown into sections, each with the heading path above it."""
    path: list[str] = []
    body: list[str] = []
    current: tuple[str, ...] = ()

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match is None:
            body.append(line)
            continue
        # A heading closes the section before it.
        joined = "\n".join(body).strip()
        if joined:
            yield _Section(headings=current, body=joined)
        body = []
        level = len(match.group(1))
        title = match.group(2).strip()
        # Truncate the path to this heading's depth, then extend. A document
        # that jumps from h1 to h3 is common and must not lose the h1.
        del path[level - 1 :]
        while len(path) < level - 1:
            path.append("")
        path.append(title)
        current = tuple(part for part in path if part)

    joined = "\n".join(body).strip()
    if joined:
        yield _Section(headings=current, body=joined)


@dataclass(frozen=True, slots=True)
class _Atom:
    """The smallest unit that must not be split further.

    Atoms are sentences, or hard-cut fragments where a single "sentence" was
    longer than a whole chunk. Everything above this works only in whole atoms,
    which is what keeps the packing loop from cutting mid-sentence.

    Keeping them atomic rather than pre-packing them into near-full pieces is
    the fix for a bug this had first time round: a pre-packed piece leaves the
    packing loop no room, so the overlap could only be *appended* to an already
    full chunk. Chunks then ran over the limit and no overlap was produced.
    """

    text: str
    #: Whether this atom begins a new paragraph, which decides the joiner. A
    #: paragraph break inside a chunk is meaningful in markdown and collapsing
    #: it to a space runs headings, list items and table rows together.
    starts_paragraph: bool


def _atoms(body: str, limit: int) -> list[_Atom]:
    """One section's body as atoms, splitting only where it must."""
    atoms: list[_Atom] = []
    for paragraph in re.split(r"\n\s*\n", body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        first = True
        for sentence in _SENTENCE_END.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            # A single sentence longer than a whole chunk — a table row, a
            # base64 blob, a language that does not use spaces. Cut on
            # whitespace where there is any, mid-token only where there is not.
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit)
                if cut <= 0:
                    cut = limit
                atoms.append(_Atom(sentence[:cut].strip(), starts_paragraph=first))
                sentence = sentence[cut:].strip()
                first = False
            if sentence:
                atoms.append(_Atom(sentence, starts_paragraph=first))
                first = False
    return atoms


def _pack(atoms: list[_Atom], limit: int, overlap: int) -> list[str]:
    """Fill chunks with whole atoms, seeding each with the tail of the last.

    Every chunk is at most ``limit`` characters, overlap included. That bound is
    what makes the overlap *best effort*: when the next atom is itself nearly a
    full chunk, there is no room for context and the context is what gives way.
    The alternative — letting the chunk run over — is how the first version of
    this produced chunks half again as large as configured.
    """
    chunks: list[str] = []
    current = ""

    for atom in atoms:
        joiner = "\n\n" if atom.starts_paragraph else " "
        candidate = f"{current}{joiner}{atom.text}" if current else atom.text
        if len(candidate) <= limit or not current:
            current = candidate
            continue

        chunks.append(current)
        seed = _tail(current, overlap)
        if seed and len(seed) + len(joiner) + len(atom.text) > limit:
            seed = ""
        current = f"{seed}{joiner}{atom.text}" if seed else atom.text

    if current:
        chunks.append(current)
    return chunks


def _tail(text: str, overlap: int) -> str:
    """The last ``overlap`` characters, starting at a word boundary."""
    if overlap <= 0 or not text:
        return ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def chunk_markdown(
    text: str,
    *,
    chunk_chars: int = 1200,
    chunk_overlap: int = 150,
) -> list[Chunk]:
    """Split extracted markdown into passages, best boundary first.

    ``chunk_chars`` bounds the *body*; the heading prefix is added afterwards
    and may push a stored chunk slightly over it. That is the right way round —
    clipping a heading to satisfy a character budget would defeat the reason it
    is there — but it means a caller sizing an embedding batch should read the
    stored text rather than assume the limit.
    """
    if chunk_chars < MIN_CHUNK_CHARS:
        raise ValueError(f"chunk_chars must be at least {MIN_CHUNK_CHARS}")
    # See the module docstring: more overlap than this and the index is mostly
    # duplicates. Clamped rather than refused, because it is a tuning knob and
    # an operator setting it high has made a judgement, not an error.
    overlap = max(0, min(chunk_overlap, chunk_chars // 3))

    bodies: list[tuple[tuple[str, ...], str]] = []
    for section in _sections(text):
        packed = _pack(_atoms(section.body, chunk_chars), chunk_chars, overlap)
        for index, body in enumerate(packed):
            # A trailing fragment joins the chunk before it rather than standing
            # alone — but only within its own section, because merging across a
            # heading would attach text to the wrong path.
            is_last = index == len(packed) - 1
            if (
                is_last
                and index > 0
                and len(body) < MIN_CHUNK_CHARS
                and bodies
                and bodies[-1][0] == section.headings
            ):
                headings, previous = bodies.pop()
                bodies.append((headings, f"{previous}\n\n{body}"))
            else:
                bodies.append((section.headings, body))

    chunks: list[Chunk] = []
    for ordinal, (headings, body) in enumerate(bodies):
        prefix = "".join(f"{'#' * (depth + 1)} {title}\n" for depth, title in enumerate(headings))
        stored = f"{prefix}\n{body}" if prefix else body
        chunks.append(Chunk(ordinal=ordinal, text=stored, headings=headings))
    return chunks
