"""Keyword lists: whole words and phrases, found in linear time (ADR 0113, section 0c).

An administrator types a comma-separated list; this finds it in a text. **No
regular expression is built from what was typed**: a list is a trie of word
tokens, and the text is walked once, so a hostile list or a hostile text cannot
make the match take more than ``len(text) * MAX_KEYWORD_TOKENS`` steps.

What a "word" is: a run of letters and digits (``\\w+``). A phrase is several
words in a row, with only spacing and punctuation between them (at most
``MAX_GAP`` characters), so "data protection" finds "Data-protection" and
"data  protection" but not "data, and protection".

Unless the list is case sensitive, comparison ignores case **and folds accents**:
the list "perche" finds "Perché", and "Müller" finds "muller". Folding is done per
token, so the offsets that come back are the original text's own.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from llmp_shared import EntitySpan

from gateway.config import KeywordList

_TOKEN = re.compile(r"\w+")
#: The most characters of spacing or punctuation that may sit between two words of a phrase.
MAX_GAP = 8


def fold(token: str) -> str:
    """Case-insensitive, accent-insensitive form of one word."""
    decomposed = unicodedata.normalize("NFKD", token.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass(slots=True)
class _Node:
    children: dict[str, _Node] = field(default_factory=dict)
    #: A phrase ends here.
    end: bool = False


def _build(words: tuple[str, ...], case_sensitive: bool) -> _Node:
    root = _Node()
    for word in words:
        node = root
        for token in _TOKEN.findall(word):
            node = node.children.setdefault(token if case_sensitive else fold(token), _Node())
        node.end = True
    return root


@lru_cache(maxsize=1024)
def _trie(words: tuple[str, ...], case_sensitive: bool) -> _Node:
    """One trie per distinct list per process; bounded because the key is operator input."""
    return _build(words, case_sensitive)


def keyword_spans(text: str, item: KeywordList) -> list[EntitySpan]:
    """Spans of *item*'s words and phrases in *text*: the longest phrase at each start.

    Non-overlapping: after a match the walk resumes past it, so one word is
    never reported twice by two phrases that share it.
    """
    root = _trie(tuple(item.words), item.case_sensitive)
    tokens = [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text)]
    keys = [tok if item.case_sensitive else fold(tok) for _, _, tok in tokens]
    spans: list[EntitySpan] = []
    i = 0
    n = len(tokens)
    while i < n:
        node = root.children.get(keys[i])
        if node is None:
            i += 1
            continue
        last = i if node.end else -1
        j = i + 1
        while j < n and node is not None:
            if tokens[j][0] - tokens[j - 1][1] > MAX_GAP:
                break
            node = node.children.get(keys[j])
            if node is None:
                break
            if node.end:
                last = j
            j += 1
        if last == -1:
            i += 1
            continue
        spans.append(
            EntitySpan(
                start=tokens[i][0],
                end=tokens[last][1],
                entity_type=item.name,
                score=1.0,
                engine="pattern",
            )
        )
        i = last + 1
    return spans
