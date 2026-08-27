"""Per-entity redaction policy: what is acted on, and how (ADR 0037).

The properties worth pinning are the ones a wrong answer breaks silently. A
policy that redacts too much destroys the request and nobody sees an error — the
measurement this feature came from is in the first test. A policy that redacts
too little leaves personal data in a prompt that has already left the building.
So both directions are asserted, and so is the one ordering subtlety that makes
them interact: filtering happens before overlap resolution.
"""

from __future__ import annotations

import time

import pytest
from gateway.config import (
    DEFAULT_REDACTION_POLICY,
    CustomPattern,
    EntityMode,
    EntityPolicy,
    RedactionPolicy,
    RedactionSettings,
)
from gateway.redaction.http import apply_spans
from llmp_shared import EntitySpan, PlaceholderMap
from pydantic import ValidationError

KEY = b"test-placeholder-key"


def redact(
    text: str,
    spans: list[EntitySpan],
    policy: RedactionPolicy | None = None,
    threshold: float = 0.0,
) -> tuple[str, int, PlaceholderMap]:
    placeholders = PlaceholderMap()
    result, count = apply_spans(
        text,
        spans,
        key=KEY,
        placeholders=placeholders,
        policy=policy,
        default_threshold=threshold,
    )
    return result, count, placeholders


def span(start: int, end: int, entity_type: str, score: float = 0.9) -> EntitySpan:
    return EntitySpan(start=start, end=end, entity_type=entity_type, score=score)


class TestTheDefaultPolicy:
    def test_a_news_site_survives_the_request_that_asked_for_it(self) -> None:
        """The measurement this whole feature came from.

        "Riassumi le notizie del giorno da ilpost.it" reached the provider as
        "<PERSON_…> le notizie del giorno da <URL_…>" — the source the user asked
        to be summarised, replaced by a token meaning nothing to the model. A URL
        is context, not identity.
        """
        text = "Riassumi le notizie del giorno da ilpost.it"
        result, count, _ = redact(text, [span(33, 42, "URL", score=0.5)])

        assert result == text
        assert count == 0

    @pytest.mark.parametrize("entity_type", ["URL", "DATE_TIME", "LOCATION", "NRP"])
    def test_the_four_context_types_are_off(self, entity_type: str) -> None:
        assert DEFAULT_REDACTION_POLICY.mode_for(entity_type) is EntityMode.OFF

    def test_everything_else_is_still_redacted_and_restorable(self) -> None:
        """Default *on*, not a curated allowlist.

        A list of what to protect silently omits whatever the detector learns
        next; the failure direction here has to be over-protection.
        """
        result, count, placeholders = redact(
            "write to mario@example.org", [span(9, 26, "EMAIL_ADDRESS")]
        )
        assert count == 1
        assert "<EMAIL_ADDRESS_" in result
        assert placeholders.restore(result) == "write to mario@example.org"

    def test_an_entity_type_nobody_has_ruled_on_is_protected(self) -> None:
        """A recogniser added by the next release of the engine, in other words."""
        result, count, _ = redact("id NEWTYPE-1", [span(3, 12, "SOMETHING_NEW")])
        assert count == 1
        assert "<SOMETHING_NEW_" in result


class TestModes:
    def test_anonymise_restore_puts_the_real_value_back(self) -> None:
        result, _, placeholders = redact(
            "Mario Rossi called",
            [span(0, 11, "PERSON")],
            RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)}),
        )
        assert "<PERSON_" in result
        assert placeholders.restore(result) == "Mario Rossi called"

    def test_anonymise_keeps_the_placeholder_in_the_answer(self) -> None:
        """Same token upstream, and the reader sees it too.

        The distinction is entirely in what the map remembers: RestoreStage
        replaces only placeholders it was told about, so not recording one is how
        a value is kept out of the answer as well as out of the prompt.
        """
        result, _, placeholders = redact(
            "Mario Rossi called",
            [span(0, 11, "PERSON")],
            RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE)}),
        )
        assert "<PERSON_" in result
        assert placeholders.restore(result) == result
        assert len(placeholders) == 0

    def test_redact_is_opaque_and_collapses_two_people(self) -> None:
        """The strongest mode, and the lossiest: the model cannot tell them apart."""
        policy = RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})
        result, count, placeholders = redact(
            "Mario wrote to Lucia",
            [span(0, 5, "PERSON"), span(15, 20, "PERSON")],
            policy,
        )
        assert result == "<PERSON> wrote to <PERSON>"
        assert count == 2
        assert len(placeholders) == 0

    def test_off_leaves_the_text_exactly_as_written(self) -> None:
        result, count, _ = redact(
            "Mario Rossi called",
            [span(0, 11, "PERSON")],
            RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.OFF)}),
        )
        assert (result, count) == ("Mario Rossi called", 0)


class TestThresholds:
    def test_a_type_can_demand_more_confidence_than_the_rest(self) -> None:
        policy = RedactionPolicy(
            entities={"PERSON": EntityPolicy(threshold=0.85), "EMAIL_ADDRESS": EntityPolicy()}
        )
        result, count, _ = redact(
            "Riassumi and bob@example.org",
            [span(0, 8, "PERSON", score=0.6), span(13, 28, "EMAIL_ADDRESS", score=0.6)],
            policy,
            threshold=0.5,
        )
        assert count == 1
        assert result.startswith("Riassumi ")
        assert "<EMAIL_ADDRESS_" in result

    def test_without_its_own_threshold_the_global_one_applies(self) -> None:
        _, count, _ = redact(
            "Riassumi", [span(0, 8, "PERSON", score=0.6)], RedactionPolicy(), threshold=0.85
        )
        assert count == 0


class TestAllowList:
    def test_a_named_value_is_never_redacted(self) -> None:
        policy = RedactionPolicy(allow_list=["ilpost.it"], entities={"URL": EntityPolicy()})
        result, count, _ = redact("read ilpost.it", [span(5, 14, "URL")], policy)
        assert (result, count) == ("read ilpost.it", 0)

    def test_it_is_case_insensitive_but_not_a_substring_rule(self) -> None:
        """ "it" allowing every Italian domain is the failure being avoided."""
        policy = RedactionPolicy(allow_list=["IlPost.IT"], entities={"URL": EntityPolicy()})
        allowed, _, _ = redact("read ilpost.it", [span(5, 14, "URL")], policy)
        assert allowed == "read ilpost.it"

        narrow = RedactionPolicy(allow_list=["it"], entities={"URL": EntityPolicy()})
        _, count, _ = redact("read ilpost.it", [span(5, 14, "URL")], narrow)
        assert count == 1


class TestOrdering:
    def test_a_disabled_span_does_not_take_an_enabled_one_with_it(self) -> None:
        """Filtering happens before overlap resolution, and the order matters.

        A high-scoring URL covering the same characters as a PERSON would win the
        overlap and then be discarded by the policy, leaving the person in the
        prompt — protection removed by a rule that was meant to remove noise.
        """
        policy = RedactionPolicy(entities={"URL": EntityPolicy(mode=EntityMode.OFF)})
        result, count, _ = redact(
            "mail mario@example.org now",
            [
                span(5, 22, "URL", score=0.95),
                span(5, 22, "PERSON", score=0.60),
            ],
            policy,
        )
        assert count == 1
        assert "<PERSON_" in result


class TestWhatTheDetectorIsAsked:
    def test_an_enumerated_policy_narrows_the_request(self) -> None:
        policy = RedactionPolicy(
            default_mode=EntityMode.OFF,
            entities={"PERSON": EntityPolicy(), "URL": EntityPolicy(mode=EntityMode.OFF)},
        )
        assert policy.detected_types() == ["PERSON"]

    def test_a_default_on_policy_asks_for_everything(self) -> None:
        """An engine's entity list is its own and cannot be enumerated here."""
        assert DEFAULT_REDACTION_POLICY.detected_types() is None

    def test_the_environment_variable_still_means_what_it_did(self) -> None:
        settings = RedactionSettings(entity_types=["PERSON", "EMAIL_ADDRESS"])
        assert settings.policy.detected_types() == ["EMAIL_ADDRESS", "PERSON"]
        assert settings.policy.mode_for("URL") is EntityMode.OFF
        assert settings.policy.mode_for("PERSON") is EntityMode.ANONYMISE_RESTORE

    def test_an_explicit_policy_wins_over_the_variable(self) -> None:
        settings = RedactionSettings(
            entity_types=["PERSON"],
            policy=RedactionPolicy(entities={"URL": EntityPolicy()}),
        )
        assert settings.policy.mode_for("URL") is EntityMode.ANONYMISE_RESTORE


class TestWeakening:
    """Which changes need a reason. The rule the API enforces."""

    def test_turning_a_type_off_weakens(self) -> None:
        before = RedactionPolicy(entities={"PERSON": EntityPolicy()})
        after = RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.OFF)})
        assert after.weakens(before)
        assert not before.weakens(after)

    def test_downgrading_a_mode_weakens(self) -> None:
        before = RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})
        after = RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE)})
        assert after.weakens(before)

    def test_exempting_more_values_weakens(self) -> None:
        before = RedactionPolicy(allow_list=["example.org"])
        after = RedactionPolicy(allow_list=["example.org", "acme.test"])
        assert after.weakens(before)
        assert not before.weakens(after)

    def test_turning_the_default_off_weakens(self) -> None:
        after = RedactionPolicy(default_mode=EntityMode.OFF)
        assert after.weakens(DEFAULT_REDACTION_POLICY)

    def test_saying_the_same_thing_twice_does_not(self) -> None:
        assert not DEFAULT_REDACTION_POLICY.weakens(DEFAULT_REDACTION_POLICY)


class TestCustomPatterns:
    """Operator-written regexes, treated as one more entity type (ADR 0038)."""

    def test_a_pattern_is_matched_and_placeheld_like_anything_else(self) -> None:
        policy = RedactionPolicy(
            patterns=[CustomPattern(name="PRJ", regex=r"PRJ-\d{4}", mode=EntityMode.REDACT)]
        )
        result, count, _ = redact("see PRJ-2043 for detail", [], policy)

        assert result == "see <PRJ> for detail"
        assert count == 1

    def test_a_pattern_can_anonymise_and_restore(self) -> None:
        policy = RedactionPolicy(
            patterns=[
                CustomPattern(name="PRJ", regex=r"PRJ-\d{4}", mode=EntityMode.ANONYMISE_RESTORE)
            ]
        )
        result, _, placeholders = redact("see PRJ-2043", [], policy)

        assert "<PRJ_" in result
        assert placeholders.restore(result) == "see PRJ-2043"

    def test_a_pattern_and_a_detector_span_compete_on_the_same_terms(self) -> None:
        """Merged before overlap resolution, so the winner is decided once.

        A hand-written pattern scores 1.0: somebody naming a value explicitly is
        not guessing, and outranks the model's opinion about the same characters.
        """
        policy = RedactionPolicy(
            patterns=[CustomPattern(name="PRJ", regex=r"PRJ-\d{4}", mode=EntityMode.REDACT)]
        )
        result, count, _ = redact(
            "see PRJ-2043",
            [span(4, 12, "PERSON", score=0.85)],
            policy,
        )

        assert (result, count) == ("see <PRJ>", 1)

    def test_the_allow_list_still_wins(self) -> None:
        policy = RedactionPolicy(
            patterns=[CustomPattern(name="PRJ", regex=r"PRJ-\d{4}")],
            allow_list=["PRJ-0000"],
        )
        result, count, _ = redact("see PRJ-0000", [], policy)

        assert (result, count) == ("see PRJ-0000", 0)

    def test_a_pattern_that_cannot_compile_is_refused_at_save_time(self) -> None:
        """RE2 has no backreferences, and its own message says which construct
        it refused — which is what an author needs to fix it."""
        with pytest.raises(ValidationError) as caught:
            CustomPattern(name="BAD", regex=r"(a)\1")

        assert "cannot be used" in str(caught.value)

    def test_a_pattern_matching_the_empty_string_is_refused(self) -> None:
        """It would match at every position and replace the whole text."""
        with pytest.raises(ValidationError) as caught:
            CustomPattern(name="EVERYTHING", regex=r"x*")

        assert "everywhere" in str(caught.value)

    def test_the_backtracking_pattern_that_hangs_python_re_is_harmless(self) -> None:
        """`re.search(r"(a+)+$", "a"*26 + "!")` takes 10.8s and cannot be
        interrupted. Under RE2 it is microseconds, which is why user-written
        patterns are allowed at all."""
        policy = RedactionPolicy(
            patterns=[CustomPattern(name="EVIL", regex=r"(a+)+$", mode=EntityMode.REDACT)]
        )
        start = time.monotonic()
        redact("a" * 26 + "!", [], policy)

        assert time.monotonic() - start < 1.0
