"""The deterministic placeholder scheme.

The properties tested here are the contract that makes multi-turn redaction work
without any session state: same entity in, same placeholder out — in another turn,
another request, another process, next week.
"""

from __future__ import annotations

import pytest
from llmp_shared.redaction import (
    PLACEHOLDER_RE,
    DetectionRequest,
    DetectionResponse,
    EntitySpan,
    PlaceholderMap,
    TextFindings,
    normalise_entity,
    placeholder_for,
)

KEY = b"a-test-hmac-key"
OTHER_KEY = b"a-different-key"


class TestDeterminism:
    def test_same_entity_yields_the_same_placeholder(self) -> None:
        """The core property: no state anywhere, yet stable across calls."""
        first = placeholder_for("PERSON", "Mario Rossi", key=KEY)
        second = placeholder_for("PERSON", "Mario Rossi", key=KEY)
        assert first == second

    def test_stable_across_independent_processes(self) -> None:
        """Nothing is cached, so two processes with the same key must agree.

        Simulated by importing the function fresh — there is no module-level state
        that could be carrying the answer between calls.
        """
        import importlib

        module = importlib.reload(importlib.import_module("llmp_shared.redaction"))
        assert module.placeholder_for("PERSON", "Mario Rossi", key=KEY) == placeholder_for(
            "PERSON", "Mario Rossi", key=KEY
        )

    def test_different_entities_differ(self) -> None:
        assert placeholder_for("PERSON", "Mario Rossi", key=KEY) != placeholder_for(
            "PERSON", "Anna Bianchi", key=KEY
        )

    def test_entity_type_is_part_of_the_identity(self) -> None:
        """The same string as two different types must not collide."""
        assert placeholder_for("PERSON", "Ada", key=KEY) != placeholder_for(
            "LOCATION", "Ada", key=KEY
        )

    def test_type_boundary_cannot_be_confused(self) -> None:
        """Domain separation: 'AB' + 'C' must not equal 'A' + 'BC'."""
        assert placeholder_for("AB", "C", key=KEY) != placeholder_for("A", "BC", key=KEY)

    def test_key_rotation_changes_every_placeholder(self) -> None:
        assert placeholder_for("PERSON", "Mario Rossi", key=KEY) != placeholder_for(
            "PERSON", "Mario Rossi", key=OTHER_KEY
        )

    def test_placeholder_does_not_leak_the_original(self) -> None:
        placeholder = placeholder_for("PERSON", "Mario Rossi", key=KEY)
        assert "Mario" not in placeholder
        assert "Rossi" not in placeholder


class TestFormat:
    def test_matches_the_discovery_pattern(self) -> None:
        """The generator and the pattern must not drift apart."""
        for entity_type, value in [
            ("PERSON", "Ada Lovelace"),
            ("EMAIL_ADDRESS", "ada@example.org"),
            ("PHONE_NUMBER", "+39 011 227 1234"),
            ("IT_FISCAL_CODE", "RSSMRA85M01H501Z"),
        ]:
            placeholder = placeholder_for(entity_type, value, key=KEY)
            assert PLACEHOLDER_RE.fullmatch(placeholder), placeholder

    def test_token_is_base32_alphabet_only(self) -> None:
        """No '+', '/' or '=': those interact badly with markdown and URLs."""
        placeholder = placeholder_for("PERSON", "Ada", key=KEY)
        token = placeholder.split("_")[-1].rstrip(">")
        assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")

    def test_length_is_configurable(self) -> None:
        short = placeholder_for("PERSON", "Ada", key=KEY, length=4)
        long = placeholder_for("PERSON", "Ada", key=KEY, length=20)
        assert len(short) < len(long)
        assert PLACEHOLDER_RE.fullmatch(short)
        assert PLACEHOLDER_RE.fullmatch(long)

    def test_engine_labels_are_normalised(self) -> None:
        """Engines disagree on labels; the placeholder must stay well-formed."""
        for label in ["phone number", "phone-number", "Phone_Number", "phone.number"]:
            placeholder = placeholder_for(label, "123", key=KEY)
            assert PLACEHOLDER_RE.fullmatch(placeholder), placeholder

    def test_label_starting_with_a_digit_is_still_valid(self) -> None:
        placeholder = placeholder_for("1099_FORM", "x", key=KEY)
        assert PLACEHOLDER_RE.fullmatch(placeholder)

    def test_rejects_an_empty_key(self) -> None:
        with pytest.raises(ValueError):
            placeholder_for("PERSON", "Ada", key=b"")

    def test_rejects_absurd_lengths(self) -> None:
        for bad in (0, 3, 33):
            with pytest.raises(ValueError):
                placeholder_for("PERSON", "Ada", key=KEY, length=bad)

    def test_rejects_an_unusable_label(self) -> None:
        with pytest.raises(ValueError):
            placeholder_for("!!!", "Ada", key=KEY)


class TestNormalisation:
    def test_case_and_whitespace_are_folded(self) -> None:
        assert normalise_entity("PERSON", "  Mario   Rossi ") == normalise_entity(
            "PERSON", "mario rossi"
        )

    def test_the_same_person_written_differently_gets_one_placeholder(self) -> None:
        assert placeholder_for("PERSON", "MARIO ROSSI", key=KEY) == placeholder_for(
            "PERSON", "  mario  rossi  ", key=KEY
        )

    def test_phone_numbers_are_compared_by_digits(self) -> None:
        """Formatting varies between turns; identity does not."""
        canonical = placeholder_for("PHONE_NUMBER", "+390112271234", key=KEY)
        for variant in ["+39 011 227 1234", "+39-011-227-1234", "(39) 011 227 1234"]:
            assert placeholder_for("PHONE_NUMBER", variant, key=KEY) == canonical

    def test_digit_stripping_does_not_collapse_unparseable_values(self) -> None:
        """Two different digitless values must not become one placeholder."""
        assert placeholder_for("PHONE_NUMBER", "unknown", key=KEY) != placeholder_for(
            "PHONE_NUMBER", "withheld", key=KEY
        )

    def test_unicode_compatibility_forms_are_folded(self) -> None:
        # The fullwidth 'm' is deliberate: NFKC must fold it onto plain 'm'.
        assert normalise_entity("PERSON", "ｍario") == normalise_entity(  # noqa: RUF001
            "PERSON", "mario"
        )


class TestPlaceholderMap:
    def test_restores_known_placeholders(self) -> None:
        mapping = PlaceholderMap()
        placeholder = placeholder_for("PERSON", "Mario Rossi", key=KEY)
        mapping.add("PERSON", "Mario Rossi", placeholder)
        assert mapping.restore(f"Hello {placeholder}!") == "Hello Mario Rossi!"

    def test_leaves_unknown_placeholders_alone(self) -> None:
        mapping = PlaceholderMap()
        unknown = placeholder_for("PERSON", "Someone Else", key=KEY)
        assert mapping.restore(f"Hello {unknown}") == f"Hello {unknown}"

    def test_empty_map_is_a_no_op(self) -> None:
        mapping = PlaceholderMap()
        assert not mapping
        assert mapping.restore("unchanged") == "unchanged"

    def test_restores_several_occurrences(self) -> None:
        mapping = PlaceholderMap()
        placeholder = placeholder_for("PERSON", "Ada", key=KEY)
        mapping.add("PERSON", "Ada", placeholder)
        assert mapping.restore(f"{placeholder} and {placeholder}") == "Ada and Ada"

    def test_lookup_by_placeholder(self) -> None:
        mapping = PlaceholderMap()
        mapping.add("PERSON", "Ada", "<PERSON_AAAABBBB>")
        assert mapping.original_for("<PERSON_AAAABBBB>") == "Ada"
        assert mapping.original_for("<PERSON_MISSING>") is None

    def test_first_mapping_wins(self) -> None:
        """Two spellings of one entity must not fight over the placeholder."""
        mapping = PlaceholderMap()
        mapping.add("PERSON", "Mario Rossi", "<PERSON_AAAABBBB>")
        mapping.add("PERSON", "mario rossi", "<PERSON_AAAABBBB>")
        assert len(mapping) == 1


class TestContractModels:
    def test_entity_span_slices_the_source_text(self) -> None:
        span = EntitySpan(start=6, end=17, entity_type="PERSON", score=0.9)
        assert span.slice_of("Hello Mario Rossi!") == "Mario Rossi"

    def test_detection_request_defaults(self) -> None:
        request = DetectionRequest(texts=["hello"])
        assert request.language == "en"
        assert request.score_threshold == 0.5
        assert request.entity_types is None
        assert request.presidio_pattern_matching is None
        assert request.presidio_ner is None

    def test_detection_response_records_the_engine(self) -> None:
        """Auditors need to know which engine version produced a redaction."""
        response = DetectionResponse(
            findings=[TextFindings(index=0, spans=[])],
            engine="presidio",
            engine_version="2.2.363",
        )
        assert response.engine == "presidio"
        assert response.findings[0].index == 0

    def test_score_is_bounded(self) -> None:
        with pytest.raises(ValueError):
            EntitySpan(start=0, end=1, entity_type="PERSON", score=1.5)
