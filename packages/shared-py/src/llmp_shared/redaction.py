"""Wire contract for PII detection, and the deterministic placeholder scheme.

Design (see ADR 0012):

The *engine* only ever detects spans. It never invents placeholder text. The
gateway performs substitution itself using :func:`placeholder_for`. That split is
what lets Presidio be swapped for another engine without changing a single
placeholder that has already been shown to a user or stored in a transcript.

Placeholders are a keyed HMAC of the normalised entity value, so:

* the same entity yields the same placeholder in turn 1 and in turn 40,
* and across separate requests, users and processes,
* with **no session state anywhere** — two processes holding the same key derive
  the same answer independently.

That property is what keeps a multi-turn conversation coherent for the upstream
model: it sees ``<PERSON_K3QF7RZM2A>`` consistently and can reason about "that
person" without ever receiving the real name.

Note the asymmetry, which is deliberate and is the whole reason the scheme works
without a database: deriving a placeholder is stateless, but *restoring* the
original value is not, because an HMAC cannot be inverted. Restoration uses the
:class:`PlaceholderMap` built while redacting the current request. That is
sufficient, because any placeholder appearing in a response must have entered the
conversation through the request that is being served.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, Field

# Matches the placeholders emitted by placeholder_for(). Used to find them again
# in model output during restoration.
PLACEHOLDER_RE: Final = re.compile(r"<([A-Z][A-Z0-9_]*?)_([A-Z2-7]{4,32})>")

# HMAC domain separator. 0x1f (unit separator) cannot occur in an entity type
# name, so "AB" + "C" can never collide with "A" + "BC".
_SEP: Final = b"\x1f"

_WHITESPACE_RE: Final = re.compile(r"\s+")
_NON_DIGIT_RE: Final = re.compile(r"\D+")

# Entity types where only the digits carry identity, so that "+39 011 227 6xxx",
# "0112276xxx" and "+390112276xxx" collapse to one placeholder.
_DIGITS_ONLY_TYPES: Final = frozenset(
    {"PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE", "IT_FISCAL_CODE", "US_SSN"}
)


def normalise_entity(entity_type: str, value: str) -> str:
    """Reduce an entity to the form that determines its identity.

    Normalisation is what makes the scheme robust to the same person or number
    being written differently in different turns. It must be stable forever: if
    you change it, previously issued placeholders stop matching, so treat this
    function as part of the persisted data format, not as an implementation
    detail.
    """
    # NFKC folds compatibility forms (e.g. fullwidth characters) together.
    text = unicodedata.normalize("NFKC", value).strip()

    if entity_type.upper() in _DIGITS_ONLY_TYPES:
        digits = _NON_DIGIT_RE.sub("", text)
        # Keep the raw text if stripping left nothing, so we never map every
        # unparseable value onto one placeholder.
        return digits or _WHITESPACE_RE.sub(" ", text).casefold()

    return _WHITESPACE_RE.sub(" ", text).casefold()


def placeholder_for(
    entity_type: str,
    value: str,
    *,
    key: bytes,
    length: int = 10,
) -> str:
    """Derive the deterministic placeholder for one entity occurrence.

    Args:
        entity_type: Engine-independent entity label, e.g. ``PERSON``.
        value: The raw matched text.
        key: Secret HMAC key. Rotating it re-labels every entity, so it must be
            stored alongside the transcripts it was used for.
        length: Base32 characters of digest to keep. 10 characters is 50 bits;
            collisions within one conversation are not a practical concern, and a
            collision degrades to two entities sharing a label rather than to any
            disclosure.

    Returns:
        A token such as ``<PERSON_K3QF7RZM2A>``.
    """
    if not key:
        raise ValueError("placeholder key must not be empty")
    if not 4 <= length <= 32:
        raise ValueError("length must be between 4 and 32")

    label = _safe_label(entity_type)
    message = label.encode() + _SEP + normalise_entity(label, value).encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()

    # Base32 keeps the token alphanumeric and uppercase. Base64 would introduce
    # '+', '/' and '=', which interact badly with markdown, URLs and tokenizers.
    token = base64.b32encode(digest).decode("ascii").rstrip("=")[:length]
    return f"<{label}_{token}>"


def opaque_placeholder(entity_type: str) -> str:
    """The label-only placeholder, e.g. ``<PERSON>``.

    What "redact" means, as distinct from "anonymise": no derived token, so two
    people in one prompt become the same ``<PERSON>`` and the model cannot tell
    them apart. That loss is the point of choosing it — the strongest option on
    the screen, and the one that costs the answer the most.

    Shares :func:`_safe_label` with :func:`placeholder_for` deliberately: both
    end up in text a restore pass reads, and two label spellings would mean a
    placeholder that cannot be matched back.
    """
    return f"<{_safe_label(entity_type)}>"


def _safe_label(entity_type: str) -> str:
    """Coerce an engine's entity label into ``[A-Z][A-Z0-9_]*``.

    Engines disagree on labels (Presidio ``PHONE_NUMBER``, others ``phone``), and
    the label ends up inside a placeholder that must round-trip through
    PLACEHOLDER_RE.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", entity_type).strip("_").upper()
    if not cleaned:
        raise ValueError(f"entity_type has no usable characters: {entity_type!r}")
    if not cleaned[0].isalpha():
        cleaned = f"E_{cleaned}"
    return cleaned


class EntitySpan(BaseModel):
    """One detected entity, as a half-open character range over the input text."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    entity_type: str
    score: float = Field(ge=0.0, le=1.0, default=1.0)

    def slice_of(self, text: str) -> str:
        return text[self.start : self.end]


class TextFindings(BaseModel):
    """Spans found in one input text, identified by its position in the batch."""

    index: int = Field(ge=0)
    spans: list[EntitySpan] = Field(default_factory=list)


class DetectionRequest(BaseModel):
    """Batch detection request sent to the detection service.

    Batched because a chat request carries many messages and per-message HTTP
    round-trips would dominate latency.
    """

    texts: list[str]
    language: str = "en"
    score_threshold: float = Field(ge=0.0, le=1.0, default=0.5)
    # None means "whatever the engine is configured to look for".
    entity_types: list[str] | None = None
    # Null preserves the detector's deployment default, which keeps existing
    # clients and direct detector users on the historical all-recognizers path.
    presidio_pattern_matching: bool | None = None
    presidio_ner: bool | None = None


class DetectionResponse(BaseModel):
    findings: list[TextFindings] = Field(default_factory=list)
    # Free-form engine identification, recorded on usage rows for auditability:
    # you will want to know which engine version produced a given redaction.
    engine: str | None = None
    engine_version: str | None = None
    # Requested entity types this engine cannot serve, named so the caller can
    # say so rather than infer it from missing detections. A rule that names
    # PERSON against an engine with no NER model is otherwise indistinguishable
    # from clean text — and Presidio's own answer to such a request is a
    # ValueError, which as a 500 reads as "service down" when the service is
    # up and the *policy* is what does not fit it.
    unsupported_types: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TextEdit:
    """One substitution, in the coordinates of the text before it happened."""

    #: Where the replaced run started.
    at: int
    #: How many characters were replaced.
    was: int
    #: How many were written in their place.
    now: int


@dataclass(frozen=True, slots=True)
class Restored:
    """Restored text, and the edits that produced it."""

    text: str
    edits: tuple[TextEdit, ...] = ()

    @property
    def moved(self) -> bool:
        """Whether any offset into this text needs adjusting at all.

        False when nothing was substituted *and* when every substitution
        happened to be the same length as what it replaced — in both cases the
        old offsets are still right, and a caller can skip the work.
        """
        return any(edit.was != edit.now for edit in self.edits)

    def shift(self, index: int) -> int:
        """Where *index* ended up after the substitutions.

        An offset inside a replaced run has no exact answer — the placeholder
        it pointed into is gone — so it lands at the start of the value that
        replaced it. That only arises if a provider cited half of a
        placeholder, which would mean it cited half of an entity name.
        """
        moved = index
        for edit in self.edits:
            if edit.at + edit.was <= index:
                moved += edit.now - edit.was
            elif edit.at < index:
                # Inside the replaced run: `moved - index` is the drift from the
                # edits that came before this one, and the run's own start is
                # the only defensible landing place.
                return edit.at + (moved - index)
        return moved


class PlaceholderMap:
    """Bidirectional map for one request's entities.

    Built while redacting a request; consumed when restoring placeholders in the
    response. Deliberately per-request and in-memory: it is a cache of an
    invertible view over a non-invertible function, not a store of record.
    """

    __slots__ = ("_to_original", "_to_placeholder")

    def __init__(self) -> None:
        self._to_original: dict[str, str] = {}
        self._to_placeholder: dict[tuple[str, str], str] = {}

    def add(self, entity_type: str, original: str, placeholder: str) -> None:
        self._to_original.setdefault(placeholder, original)
        self._to_placeholder.setdefault((entity_type.upper(), original), placeholder)

    def original_for(self, placeholder: str) -> str | None:
        return self._to_original.get(placeholder)

    def placeholders(self) -> list[str]:
        """Every placeholder issued for this request.

        Used to size the stream buffer exactly: the only thing restoration
        matches is a placeholder from this list, so their longest length is the
        precise amount of text a streamed response must hold back — not an
        estimate of "the longest entity we might see".
        """
        return list(self._to_original)

    def restore(self, text: str) -> str:
        """Replace every known placeholder in *text* with its original value.

        Deliberately a literal substitution over the known placeholders rather
        than a regex sweep. Restoration must not depend on the emitted placeholder
        still matching :data:`PLACEHOLDER_RE`: if the pattern and the generator
        ever drift apart, a regex-driven restore silently stops restoring, whereas
        this cannot.

        Unknown placeholders are left untouched. They may be text the user wrote
        themselves, or something the model invented, and inventing an "original"
        for them would be worse than leaving them visible.

        Longest first, so a placeholder that is a prefix of another cannot be
        substituted inside it.
        """
        return self.restore_with_edits(text).text

    def restore_with_edits(self, text: str) -> Restored:
        """:meth:`restore`, and *where* each substitution happened.

        The positions are what a citation needs. A provider that returns
        character offsets into the assistant's text — OpenAI's ``url_citation``
        annotations do — computed them against the text it generated, which
        contained placeholders. Restoring a name that is longer or shorter than
        the placeholder moves every character after it, so the offsets have to
        move with it or they quietly point at the wrong words (ADR 0059).

        One left-to-right pass rather than a chain of :meth:`str.replace` calls,
        because a chain cannot say where it wrote. The result is the same:
        candidates are ranked longest-first, so a placeholder that is a prefix
        of another is never substituted inside it, and overlapping matches are
        resolved in favour of the longer one.
        """
        if not self._to_original:
            return Restored(text=text, edits=())

        # Every occurrence of every known placeholder, longest first so that a
        # prefix loses to the placeholder containing it.
        found: list[tuple[int, str]] = []
        for placeholder in sorted(self._to_original, key=len, reverse=True):
            start = text.find(placeholder)
            while start != -1:
                found.append((start, placeholder))
                start = text.find(placeholder, start + 1)
        if not found:
            return Restored(text=text, edits=())

        # Sorted by position, and by length descending within a position, so the
        # overlap check below keeps the longer of two matches that start together.
        found.sort(key=lambda item: (item[0], -len(item[1])))

        pieces: list[str] = []
        edits: list[TextEdit] = []
        cursor = 0
        for start, placeholder in found:
            if start < cursor:
                # Overlaps something already substituted. The earlier, longer
                # match won; this one was never really there.
                continue
            original = self._to_original[placeholder]
            pieces.append(text[cursor:start])
            pieces.append(original)
            edits.append(TextEdit(at=start, was=len(placeholder), now=len(original)))
            cursor = start + len(placeholder)
        pieces.append(text[cursor:])
        return Restored(text="".join(pieces), edits=tuple(edits))

    def __len__(self) -> int:
        return len(self._to_original)

    def __bool__(self) -> bool:
        return bool(self._to_original)
