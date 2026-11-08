"""The quick check: stateless verdicts from patterns and word lists (ADR 0113).

"Does this text contain health data / identifiers, and what language is it?"
answered in microseconds, with no model and no network. It is the default way a
content condition is decided, and the floor every model-based method falls back
to. Honest about what it is: it finds health vocabulary and well-formed
identifiers, and it misses health data written in plain words ("he has been
taking insulin since March"). A rule that must catch that chooses a model
profile (phase 2).

Nothing here keeps text. :class:`VerdictCache` is a performance optimisation
keyed by ``sha256(text)`` that stores three small fields; a miss recomputes the
same answer, so the cache can never change an outcome.

The identifier patterns follow the shapes Presidio's recognisers cover (Italian
fiscal code, VAT number, IBAN, US SSN, payment card, NHS number), each with the
checksum that makes a match worth reporting. They are written out here rather
than imported: Presidio and spaCy live in the redaction service, and must never
enter the gateway's dependency set.
"""

from __future__ import annotations

import hashlib
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

#: Only this much of a text is judged: the verdict is about whether the text is
#: that kind of text, and an embedding batch can be megabytes.
MAX_CHARS = 20_000

UNKNOWN_LANGUAGE = "und"


@dataclass(frozen=True, slots=True)
class Classification:
    """What the quick check concluded. Labels only; never a span or a value."""

    health: bool
    ids: bool
    lang: str


def _fold(text: str) -> str:
    """Lower-case, accents removed, so "anamnèsi" and "Anamnesi" are one word."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# -- health vocabulary -------------------------------------------------------------
#
# *Strong* terms say "clinical" on their own. *Weak* terms are common in other
# contexts ("therapy" in a marketing text) and count only in pairs.

_HEALTH_STRONG = (
    # Italian
    "referto",
    "referti",
    "anamnesi",
    "diagnosi",
    "cartella clinica",
    "ricovero",
    "dimissione ospedaliera",
    "esame del sangue",
    "esami del sangue",
    "esami ematochimici",
    "emocromo",
    "glicemia",
    "emoglobina glicata",
    "pressione arteriosa",
    "terapia farmacologica",
    "prescrizione medica",
    "ricetta medica",
    "tumore",
    "neoplasia",
    "oncologic",
    "chemioterapia",
    "radioterapia",
    "sieropositiv",
    "diabete",
    "ipertensione",
    "infarto",
    "patologia",
    "malattia cronica",
    "disturbo psichiatrico",
    "depressione maggiore",
    "stato di salute",
    "dati sanitari",
    "tessera sanitaria",
    # English
    "diagnosis",
    "diagnosed with",
    "medical record",
    "medical history",
    "health record",
    "patient",
    "discharge summary",
    "blood test",
    "lab results",
    "blood pressure",
    "chemotherapy",
    "radiotherapy",
    "oncology",
    "hiv positive",
    "diabetes",
    "hypertension",
    "myocardial infarction",
    "prescription",
    "psychiatric",
    "mental health",
    "clinical trial",
    "health data",
    "protected health information",
)
_HEALTH_WEAK = (
    "terapia",
    "farmaco",
    "farmaci",
    "sintomi",
    "sintomo",
    "dosaggio",
    "ospedale",
    "paziente",
    "pazienti",
    "medico",
    "dottore",
    "clinica",
    "visita",
    "allergia",
    "therapy",
    "medication",
    "symptoms",
    "symptom",
    "dosage",
    "hospital",
    "doctor",
    "physician",
    "clinic",
    "allergy",
    "treatment",
    "surgery",
)


def _word_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    # A term is matched at a word start. Terms ending in a stem ("oncologic",
    # "sieropositiv") are allowed to continue; whole words are not, so
    # "patient" does not match "patientia".
    folded = sorted({_fold(t) for t in terms}, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in folded) + r")", re.IGNORECASE)


_STRONG_RE = _word_pattern(_HEALTH_STRONG)
_WEAK_RE = _word_pattern(_HEALTH_WEAK)


def _is_health(folded: str) -> bool:
    if _STRONG_RE.search(folded):
        return True
    return len({m.group(1) for m in _WEAK_RE.finditer(folded)}) >= 2


# -- identifiers ---------------------------------------------------------------------

_CODICE_FISCALE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_VAT_CONTEXT = re.compile(
    r"(?:p\.?\s?iva|partita\s+iva|vat(?:\s+(?:number|no\.?|id))?)\W{0,3}(?:it)?\s?(\d{11})\b",
    re.IGNORECASE,
)
_NHS_CONTEXT = re.compile(r"nhs\s+(?:number|no\.?)\W{0,3}(\d{3}\s?\d{3}\s?\d{4})\b", re.IGNORECASE)

_CF_ODD = {
    **dict(zip("0123456789", (1, 0, 5, 7, 9, 13, 15, 17, 19, 21), strict=True)),
    **dict(
        zip(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            (
                1,
                0,
                5,
                7,
                9,
                13,
                15,
                17,
                19,
                21,
                2,
                4,
                18,
                20,
                11,
                3,
                6,
                8,
                12,
                14,
                16,
                10,
                22,
                25,
                24,
                23,
            ),
            strict=True,
        )
    ),
}
_CF_EVEN = {
    **{c: i for i, c in enumerate("0123456789")},
    **{c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
}


def _codice_fiscale_ok(code: str) -> bool:
    code = code.upper()
    total = sum((_CF_ODD if index % 2 == 0 else _CF_EVEN)[ch] for index, ch in enumerate(code[:15]))
    return chr(ord("A") + total % 26) == code[15]


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        n = int(ch)
        if index % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_ok(raw: str) -> bool:
    iban = raw.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34:
        return False
    moved = iban[4:] + iban[:4]
    try:
        number = int("".join(str(int(ch, 36)) for ch in moved))
    except ValueError:
        return False
    return number % 97 == 1


def _nhs_ok(raw: str) -> bool:
    digits = re.sub(r"\s", "", raw)
    total = sum(int(d) * (10 - i) for i, d in enumerate(digits[:9]))
    check = 11 - total % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(digits[9])


def _has_ids(text: str) -> bool:
    if any(_codice_fiscale_ok(m.group(0)) for m in _CODICE_FISCALE.finditer(text)):
        return True
    if any(_iban_ok(m.group(0)) for m in _IBAN.finditer(text)):
        return True
    if _SSN.search(text):
        return True
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    if any(_luhn_ok(m.group(1)) for m in _VAT_CONTEXT.finditer(text)):
        return True
    return any(_nhs_ok(m.group(1)) for m in _NHS_CONTEXT.finditer(text))


# -- language ------------------------------------------------------------------------

_STOPWORDS: dict[str, frozenset[str]] = {
    "it": frozenset(
        [
            "il",
            "lo",
            "la",
            "i",
            "gli",
            "le",
            "un",
            "uno",
            "una",
            "di",
            "a",
            "da",
            "in",
            "con",
            "su",
            "per",
            "tra",
            "fra",
            "e",
            "ed",
            "o",
            "ma",
            "che",
            "non",
            "si",
            "come",
            "anche",
            "questo",
            "questa",
            "sono",
            "della",
            "delle",
            "del",
            "dei",
            "nel",
            "nella",
            "alla",
            "al",
            "ho",
            "ha",
            "hanno",
            "è",
            "più",
            "molto",
            "quando",
            "perché",
        ]
    ),
    "en": frozenset(
        [
            "the",
            "of",
            "and",
            "to",
            "in",
            "is",
            "that",
            "it",
            "for",
            "as",
            "with",
            "was",
            "on",
            "be",
            "by",
            "this",
            "are",
            "from",
            "or",
            "an",
            "at",
            "not",
            "have",
            "has",
            "had",
            "but",
            "they",
            "you",
            "which",
            "their",
            "were",
            "been",
            "will",
            "would",
            "there",
            "what",
        ]
    ),
    "fr": frozenset(
        [
            "le",
            "la",
            "les",
            "de",
            "des",
            "du",
            "un",
            "une",
            "et",
            "en",
            "est",
            "que",
            "qui",
            "pour",
            "dans",
            "ce",
            "il",
            "elle",
            "ne",
            "pas",
            "sur",
            "au",
            "avec",
            "ses",
            "sont",
            "plus",
            "par",
            "mais",
            "ou",
            "nous",
            "vous",
        ]
    ),
    "de": frozenset(
        [
            "der",
            "die",
            "das",
            "und",
            "ist",
            "von",
            "zu",
            "den",
            "mit",
            "sich",
            "des",
            "auf",
            "für",
            "nicht",
            "ein",
            "eine",
            "dem",
            "im",
            "auch",
            "es",
            "dass",
            "sie",
            "wir",
            "ich",
            "aber",
            "wie",
            "oder",
        ]
    ),
    "es": frozenset(
        [
            "el",
            "la",
            "los",
            "las",
            "de",
            "del",
            "y",
            "en",
            "que",
            "es",
            "un",
            "una",
            "por",
            "con",
            "no",
            "se",
            "para",
            "su",
            "al",
            "lo",
            "como",
            "más",
            "pero",
            "sus",
            "le",
            "ya",
            "o",
            "este",
        ]
    ),
}
_TOKEN = re.compile(r"[a-zà-ÿ']+", re.IGNORECASE)


def detect_language(text: str) -> str:
    """A two-letter code by stop-word counts, or ``und`` when it cannot tell.

    Cheap and good enough for "is this Italian": it needs a few sentences and
    returns ``und`` for a handful of words or a text with no clear winner.
    """
    words = [w.lower() for w in _TOKEN.findall(text[:MAX_CHARS])]
    if len(words) < 4:
        return UNKNOWN_LANGUAGE
    scores = {
        lang: sum(1 for w in words if w in vocabulary) for lang, vocabulary in _STOPWORDS.items()
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    if best[1] < 2 or best[1] < runner_up[1] * 1.25 + 0.01:
        return UNKNOWN_LANGUAGE
    return best[0]


def classify(text: str) -> Classification:
    """The quick check over (the first :data:`MAX_CHARS` of) *text*."""
    clipped = text[:MAX_CHARS]
    return Classification(
        health=_is_health(_fold(clipped)),
        ids=_has_ids(clipped),
        lang=detect_language(clipped),
    )


class VerdictCache:
    """An LRU of verdicts by ``sha256(text)``: labels and a language, never text.

    A performance optimisation only (ADR 0113, section 4): a miss recomputes the
    same answer. Per process, like the detection cache beside it.
    """

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[str, Classification] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def classify(self, text: str) -> Classification:
        key = hashlib.sha256(text[:MAX_CHARS].encode("utf-8", "surrogatepass")).hexdigest()
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return found
        verdict = classify(text)
        with self._lock:
            self.misses += 1
            self._entries[key] = verdict
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return verdict

    def __len__(self) -> int:
        return len(self._entries)
