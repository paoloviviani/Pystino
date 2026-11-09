"""Keyword lists (ADR 0113, section 0c): parsing, whole-word matching, accents, linear time."""

from __future__ import annotations

import time

import pytest
from gateway.config import EntityMode, KeywordList, RedactionPolicy
from gateway.redaction.http import eligible_for, pattern_spans
from gateway.redaction.keywords import keyword_spans
from pydantic import ValidationError


def found(text: str, words: str | list[str], **kw: object) -> list[str]:
    item = KeywordList(name="K", words=words, **kw)  # type: ignore[arg-type]
    return [text[s.start : s.end] for s in keyword_spans(text, item)]


class TestParsing:
    def test_a_comma_separated_string_is_the_list(self) -> None:
        item = KeywordList(name="my list", words=" alpha,  beta gamma ,, alpha ")
        assert item.words == ["alpha", "beta gamma"] and item.name == "MY_LIST"
        assert item.case_sensitive is False and item.mode is EntityMode.REDACT

    def test_a_list_is_taken_as_it_is(self) -> None:
        assert KeywordList(name="K", words=["a", "b"]).words == ["a", "b"]

    @pytest.mark.parametrize(
        "words", ["", " , ,", "!!!", "x" * 101, "one two three four five six seven"]
    )
    def test_what_cannot_be_looked_for_is_refused(self, words: str) -> None:
        with pytest.raises(ValidationError):
            KeywordList(name="K", words=words)

    def test_the_list_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            KeywordList(name="K", words=[f"w{n}" for n in range(1001)])


class TestMatching:
    def test_whole_words_only(self) -> None:
        assert found("a cat concatenates, the cat sat", "cat") == ["cat", "cat"]

    def test_case_is_ignored_and_accents_are_folded_by_default(self) -> None:
        assert found("Perché Müller e CAFFÈ", "perche, muller, caffe") == [
            "Perché",
            "Müller",
            "CAFFÈ",
        ]

    def test_case_sensitive_means_exactly_that(self) -> None:
        assert found("Falcon falcon FALCON", "Falcon", case_sensitive=True) == ["Falcon"]
        assert found("Müller muller", "Müller", case_sensitive=True) == ["Müller"]

    def test_a_phrase_ignores_spacing_and_punctuation_between_its_words(self) -> None:
        assert found("the Blue-Heron plan, blue  heron too", "blue heron") == [
            "Blue-Heron",
            "blue  heron",
        ]

    def test_a_phrase_is_not_found_across_a_gap(self) -> None:
        assert found("blue, and then a heron", "blue heron") == []

    def test_the_longest_phrase_wins_and_nothing_is_reported_twice(self) -> None:
        assert found("blue heron nest", "blue, blue heron, heron nest") == ["blue heron"]

    def test_offsets_are_the_originals(self) -> None:
        text = "Zoë and Müller"
        spans = keyword_spans(text, KeywordList(name="K", words="Müller"))
        assert [(s.start, s.end, s.engine, s.score) for s in spans] == [(8, 14, "pattern", 1.0)]

    def test_regex_characters_in_a_word_mean_nothing(self) -> None:
        # No regular expression is built from the list: "a.c" is the words "a" and "c".
        assert found("abc a.c", "a.c") == ["a.c"]
        assert found("x" * 50, "(x+)+$") == []


class TestLinear:
    def test_a_hostile_list_and_text_stay_fast(self) -> None:
        words = [" ".join(["a"] * n) for n in range(1, 7)] + [f"w{n}" for n in range(900)]
        item = KeywordList(name="K", words=words)
        text = "a " * 500_000
        started = time.perf_counter()
        keyword_spans(text, item)
        assert time.perf_counter() - started < 10


class TestInAPolicy:
    def test_a_policy_finds_its_keywords_with_the_regular_expressions(self) -> None:
        policy = RedactionPolicy(
            keywords=[KeywordList(name="CODES", words="Falcon", mode=EntityMode.DETECT_ONLY)]
        )
        spans = pattern_spans("The Falcon lands", policy)
        assert [s.entity_type for s in spans] == ["CODES"]

    def test_off_finds_nothing(self) -> None:
        policy = RedactionPolicy(
            keywords=[KeywordList(name="CODES", words="Falcon", mode=EntityMode.OFF)]
        )
        assert pattern_spans("The Falcon lands", policy) == []

    def test_detect_only_is_found_and_never_acted_on(self) -> None:
        policy = RedactionPolicy(
            keywords=[KeywordList(name="CODES", words="Falcon", mode=EntityMode.DETECT_ONLY)]
        )
        text = "The Falcon lands"
        assert len(eligible_for(text, [], policy, 0.5, acting=False)) == 1
        assert eligible_for(text, [], policy, 0.5) == []
        assert policy.is_active() is False and policy.finds_anything() is True
