"""The HTTP detection engine and the plugin registry.

The detector is faked throughout — deliberately. What is being tested is the
gateway's half of the contract: that spans become stable placeholders, that a
stream restores them across frame boundaries, that a detector outage fails closed,
and that any service serving the contract works without gateway code. Whether
Presidio finds a fiscal code is Presidio's test, and it runs in services/redaction.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from gateway.config import EntityMode, RedactionPolicy, RedactionSettings
from gateway.redaction import (
    NoOpRedactor,
    RedactionUnavailableError,
    UnknownEngineError,
    available_engines,
    build_redactor,
)
from gateway.redaction.http import HttpDetectionRedactor, apply_spans
from gateway.sse.events import SSEEvent
from llmp_shared import EntitySpan, PlaceholderMap, placeholder_for
from pydantic import SecretStr

KEY = "test-placeholder-key"


#: A policy that acts on whatever the detector reports.
#:
#: Since ADR 0039 a deployment filters nothing until somebody writes a rule, so a
#: test about *substitution* has to say it wants substitution — otherwise it is
#: really testing the default, and every assertion below would pass against a
#: redactor that does nothing at all.
PROTECT_EVERYTHING = RedactionPolicy(default_mode=EntityMode.ANONYMISE_RESTORE)


def settings(**overrides: Any) -> RedactionSettings:
    base: dict[str, Any] = {
        "engine": "http",
        "endpoint": "http://detector:8080",
        "placeholder_key": SecretStr(KEY),
        "policy": PROTECT_EVERYTHING,
    }
    return RedactionSettings(**{**base, **overrides})


class FakeDetector:
    """A detection service that finds whatever it is told to find.

    ``spans_for`` maps a substring to an entity type; every occurrence in every
    text is reported. Enough to exercise the contract without a model.
    """

    def __init__(self, spans_for: dict[str, str] | None = None) -> None:
        self.spans_for = spans_for or {}
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.fail_with: Exception | None = None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            raise self.fail_with
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})

        findings = []
        for index, text in enumerate(payload["texts"]):
            spans = []
            for needle, entity_type in self.spans_for.items():
                start = text.find(needle)
                while start != -1:
                    spans.append(
                        {
                            "start": start,
                            "end": start + len(needle),
                            "entity_type": entity_type,
                            "score": 0.9,
                        }
                    )
                    start = text.find(needle, start + 1)
            findings.append({"index": index, "spans": spans})
        return httpx.Response(200, json={"findings": findings, "engine": "fake"})

    def redactor(self, **overrides: Any) -> HttpDetectionRedactor:
        return HttpDetectionRedactor(
            settings(**overrides),
            # A lambda, not the bound method: respond_unsupported rebinds
            # _handle, and a transport holding the original binding would
            # never see it.
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: self._handle(request))
            ),
        )

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def respond_unsupported(self, types: list[str]) -> None:
        """The engine answers 200 but names types it cannot serve."""
        import json as _json

        original = self._handle

        def with_unsupported(request: httpx.Request) -> httpx.Response:
            response = original(request)
            body = _json.loads(response.content)
            body["unsupported_types"] = types
            return httpx.Response(
                response.status_code, content=_json.dumps(body).encode(),
                headers={"content-type": "application/json"},
            )

        self._handle = with_unsupported  # type: ignore[method-assign]


class TestRegistry:
    def test_the_builtins_are_registered(self) -> None:
        assert {"noop", "http"} <= set(available_engines())

    def test_noop_builds(self) -> None:
        assert isinstance(build_redactor(RedactionSettings()), NoOpRedactor)

    def test_an_unknown_engine_names_what_is_available(self) -> None:
        """Never a silent fallback to noop: believing redaction is on when it is
        not is the worst outcome available."""
        with pytest.raises(UnknownEngineError) as caught:
            build_redactor(RedactionSettings.model_construct(engine="presidio-ish"))
        assert "noop" in str(caught.value)
        assert "llmp.redactors" in str(caught.value)

    def test_a_third_party_engine_needs_no_gateway_code(self, monkeypatch: Any) -> None:
        """The plugin promise, tested rather than asserted in an ADR."""
        from gateway.redaction import registry

        class Custom(NoOpRedactor):
            name = "custom"

        class FakeEntryPoint:
            name = "custom"

            @staticmethod
            def load() -> Any:
                return lambda _settings: Custom()

        monkeypatch.setattr(registry, "entry_points", lambda group: [FakeEntryPoint()])
        built = build_redactor(RedactionSettings.model_construct(engine="custom"))
        assert isinstance(built, Custom)
        assert "custom" in registry.available()

    def test_a_builtin_cannot_be_replaced_by_a_plugin(self, monkeypatch: Any) -> None:
        """Otherwise installing a package could quietly turn `noop` into something
        that does not do nothing."""
        from gateway.redaction import registry

        class FakeEntryPoint:
            name = "noop"

            @staticmethod
            def load() -> Any:
                raise AssertionError("must not be consulted")

        monkeypatch.setattr(registry, "entry_points", lambda group: [FakeEntryPoint()])
        assert isinstance(build_redactor(RedactionSettings()), NoOpRedactor)

    def test_a_broken_plugin_does_not_break_the_listing(self, monkeypatch: Any) -> None:
        from gateway.redaction import registry

        class Broken:
            name = "broken"

            @staticmethod
            def load() -> Any:
                raise ImportError("no such module")

        monkeypatch.setattr(registry, "entry_points", lambda group: [Broken()])
        assert "noop" in registry.available()

    def test_but_asking_for_the_broken_plugin_is_an_error(self, monkeypatch: Any) -> None:
        from gateway.redaction import registry

        class Broken:
            name = "broken"

            @staticmethod
            def load() -> Any:
                raise ImportError("no such module")

        monkeypatch.setattr(registry, "entry_points", lambda group: [Broken()])
        with pytest.raises(UnknownEngineError, match="failed to load"):
            registry.resolve("broken")


class TestConfiguration:
    def test_http_without_an_endpoint_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ENDPOINT"):
            HttpDetectionRedactor(
                RedactionSettings.model_construct(
                    engine="http", endpoint="", placeholder_key=SecretStr(KEY)
                )
            )

    def test_http_without_a_placeholder_key_is_refused(self) -> None:
        """The key is what makes placeholders stable; an empty one is not a
        default, it is a silently broken deployment."""
        with pytest.raises(ValueError, match="PLACEHOLDER_KEY"):
            HttpDetectionRedactor(
                RedactionSettings.model_construct(
                    engine="http", endpoint="http://d", placeholder_key=SecretStr("")
                )
            )


class TestRequestRedaction:
    async def test_a_detected_entity_is_replaced(self) -> None:
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        outcome = await detector.redactor().redact_request(
            [{"role": "user", "content": "Contact Mario Rossi about the grant."}]
        )
        assert "Mario Rossi" not in outcome.messages[0]["content"]
        assert "<PERSON_" in outcome.messages[0]["content"]
        assert outcome.entity_count == 1
        assert outcome.changed

    async def test_unsupported_types_are_logged_once_per_change(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The detector cannot protect what it is not asked for, so a rule
        naming an entity this engine lacks is *inert* — silent under-protection
        unless something names it. The log is that something, and it fires on
        the change, not per request, or it buries itself."""
        import logging

        detector = FakeDetector({"a@b.test": "EMAIL_ADDRESS"})
        detector.respond_unsupported(["PERSON"])
        redactor = detector.redactor()
        messages = [{"role": "user", "content": "Ask a@b.test"}]

        with caplog.at_level(logging.WARNING):
            await redactor.redact_request(messages)
            await redactor.redact_request(messages)  # same set: no repeat
            assert "PERSON" in caplog.text
            assert caplog.text.count("PERSON") == 1

            detector.respond_unsupported(["PERSON", "IBAN_CODE"])
            # Different text, or the detection cache serves the last result and
            # the engine — with its new answer — is never asked.
            await redactor.redact_request([{"role": "user", "content": "Ask a@b.test now"}])
            assert "IBAN_CODE" in caplog.text  # the change is named

    async def test_no_unsupported_types_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        detector = FakeDetector({"Mario Rossi": "PERSON"})
        with caplog.at_level(logging.WARNING):
            await detector.redactor().redact_request(
                [{"role": "user", "content": "Ask Mario Rossi."}]
            )
        assert "cannot serve" not in caplog.text

    async def test_the_callers_messages_are_not_mutated(self) -> None:
        """The transcript of what the user actually sent must survive redaction."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        original = [{"role": "user", "content": "Ask Mario Rossi."}]
        await detector.redactor().redact_request(original)
        assert original[0]["content"] == "Ask Mario Rossi."

    async def test_the_same_entity_gets_the_same_placeholder_everywhere(self) -> None:
        """The property the whole scheme exists for: the upstream can reason about
        "that person" across forty turns without ever seeing the name."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        outcome = await detector.redactor().redact_request(
            [
                {"role": "user", "content": "Mario Rossi wrote the proposal."},
                {"role": "assistant", "content": "What did Mario Rossi propose?"},
            ]
        )
        first = outcome.messages[0]["content"]
        second = outcome.messages[1]["content"]
        token = first[first.index("<") : first.index(">") + 1]
        assert token in second

    async def test_placeholders_match_the_shared_derivation(self) -> None:
        """The gateway derives them; nothing about the detector is involved."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        outcome = await detector.redactor().redact_request(
            [{"role": "user", "content": "Mario Rossi"}]
        )
        assert outcome.messages[0]["content"] == placeholder_for(
            "PERSON", "Mario Rossi", key=KEY.encode()
        )

    async def test_multimodal_content_parts_are_redacted_in_place(self) -> None:
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        outcome = await detector.redactor().redact_request(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Who is Mario Rossi?"},
                        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                    ],
                }
            ]
        )
        parts = outcome.messages[0]["content"]
        assert "Mario Rossi" not in parts[0]["text"]
        assert parts[1]["image_url"]["url"] == "http://x/y.png"

    async def test_nothing_detected_leaves_everything_alone(self) -> None:
        detector = FakeDetector()
        outcome = await detector.redactor().redact_request(
            [{"role": "user", "content": "What is the weather?"}]
        )
        assert outcome.entity_count == 0
        assert not outcome.changed
        assert outcome.messages[0]["content"] == "What is the weather?"

    async def test_all_messages_go_in_one_round_trip(self) -> None:
        """Per-message HTTP calls would dominate latency on a long conversation."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        await detector.redactor().redact_request(
            [{"role": "user", "content": f"Mario Rossi {n}"} for n in range(20)]
        )
        assert detector.call_count == 1
        assert len(detector.requests[0]["texts"]) == 20


class TestOverlappingSpans:
    def test_the_higher_scoring_span_wins(self) -> None:
        """Two detectors disagreeing about the same characters is normal; nesting
        one replacement inside another produces corrupted text."""
        placeholders = PlaceholderMap()
        text, count = apply_spans(
            "call IT01234567890 now",
            [
                EntitySpan(start=5, end=18, entity_type="IT_VAT_CODE", score=0.9),
                EntitySpan(start=7, end=18, entity_type="PHONE_NUMBER", score=0.4),
            ],
            key=KEY.encode(),
            placeholders=placeholders,
            policy=PROTECT_EVERYTHING,
        )
        assert count == 1
        assert "IT_VAT_CODE" in text
        assert "PHONE_NUMBER" not in text

    def test_adjacent_spans_are_both_replaced(self) -> None:
        placeholders = PlaceholderMap()
        text, count = apply_spans(
            "ab",
            [
                EntitySpan(start=0, end=1, entity_type="A", score=1.0),
                EntitySpan(start=1, end=2, entity_type="B", score=1.0),
            ],
            key=KEY.encode(),
            placeholders=placeholders,
            policy=PROTECT_EVERYTHING,
        )
        assert count == 2
        assert text.startswith("<A_") and "<B_" in text

    def test_a_span_past_the_end_of_the_text_is_ignored(self) -> None:
        """A detector that miscounts offsets must not be able to corrupt text."""
        placeholders = PlaceholderMap()
        text, count = apply_spans(
            "short",
            [EntitySpan(start=0, end=500, entity_type="PERSON", score=1.0)],
            key=KEY.encode(),
            placeholders=placeholders,
            policy=PROTECT_EVERYTHING,
        )
        assert (text, count) == ("short", 0)

    def test_later_spans_keep_their_offsets(self) -> None:
        """Rewriting left to right invalidates every span after the first."""
        placeholders = PlaceholderMap()
        text, count = apply_spans(
            "A said to B",
            [
                EntitySpan(start=0, end=1, entity_type="PERSON", score=1.0),
                EntitySpan(start=10, end=11, entity_type="PERSON", score=1.0),
            ],
            key=KEY.encode(),
            placeholders=placeholders,
            policy=PROTECT_EVERYTHING,
        )
        assert count == 2
        assert " said to " in text
        assert text.count("<PERSON_") == 2


class TestCaching:
    async def test_a_repeated_message_is_not_detected_again(self) -> None:
        """Turn 40 resends turns 1-39; without this, inference is quadratic."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        redactor = detector.redactor()
        history = [{"role": "user", "content": "Mario Rossi wrote it."}]

        await redactor.redact_request(history)
        assert len(detector.requests[0]["texts"]) == 1

        history.append({"role": "assistant", "content": "Yes, he did."})
        history.append({"role": "user", "content": "When?"})
        await redactor.redact_request(history)

        # Only the two new messages were sent.
        assert detector.call_count == 2
        assert detector.requests[1]["texts"] == ["Yes, he did.", "When?"]

    async def test_a_fully_cached_request_makes_no_call_at_all(self) -> None:
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        redactor = detector.redactor()
        messages = [{"role": "user", "content": "Mario Rossi"}]
        first = await redactor.redact_request(messages)
        second = await redactor.redact_request(messages)
        assert detector.call_count == 1
        assert first.messages[0]["content"] == second.messages[0]["content"]

    async def test_the_cache_cannot_change_the_result(self) -> None:
        """It is an optimisation over a pure function, which is what makes it safe."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        cached = detector.redactor()
        uncached = detector.redactor(cache_size=0)
        messages = [{"role": "user", "content": "Mario Rossi and Mario Rossi"}]

        await cached.redact_request(messages)
        again = await cached.redact_request(messages)
        without = await uncached.redact_request(messages)
        assert again.messages[0]["content"] == without.messages[0]["content"]

    async def test_the_cache_is_bounded(self) -> None:
        detector = FakeDetector()
        redactor = detector.redactor(cache_size=4)
        for n in range(20):
            await redactor.redact_request([{"role": "user", "content": f"message {n}"}])
        assert len(redactor._cache) == 4


class TestFailureHandling:
    async def test_an_outage_refuses_the_request_by_default(self) -> None:
        """Fail closed. A redaction layer that silently stops redacting is worse
        than an outage, because nobody finds out."""
        detector = FakeDetector()
        detector.fail_with = httpx.ConnectError("refused")
        with pytest.raises(RedactionUnavailableError):
            await detector.redactor().redact_request([{"role": "user", "content": "hello"}])

    async def test_an_error_status_also_refuses(self) -> None:
        detector = FakeDetector()
        detector.status = 500
        with pytest.raises(RedactionUnavailableError):
            await detector.redactor().redact_request([{"role": "user", "content": "hello"}])

    async def test_fail_open_forwards_unredacted_when_asked(self) -> None:
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        detector.fail_with = httpx.ConnectError("refused")
        outcome = await detector.redactor(fail_open=True).redact_request(
            [{"role": "user", "content": "Mario Rossi"}]
        )
        assert outcome.messages[0]["content"] == "Mario Rossi"
        assert outcome.entity_count == 0

    async def test_a_failed_detection_is_not_cached(self) -> None:
        """Or one outage would poison the cache with empty results."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        redactor = detector.redactor(fail_open=True)
        detector.fail_with = httpx.ConnectError("refused")
        await redactor.redact_request([{"role": "user", "content": "Mario Rossi"}])

        detector.fail_with = None
        outcome = await redactor.redact_request([{"role": "user", "content": "Mario Rossi"}])
        assert outcome.entity_count == 1


class TestResponseRestoration:
    async def test_the_non_streaming_response_is_restored(self) -> None:
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        redactor = detector.redactor()
        outcome = await redactor.redact_request([{"role": "user", "content": "Mario Rossi"}])
        placeholder = outcome.messages[0]["content"]

        restored = await redactor.restore_response(
            f"I will contact {placeholder} today.", outcome
        )
        assert restored.text == "I will contact Mario Rossi today."
        # The edit is what a citation needs: where it happened, and by how much
        # the text after it moved (ADR 0059).
        assert restored.moved is True
        assert [(e.at, e.was, e.now) for e in restored.edits] == [
            (15, len(placeholder), len("Mario Rossi"))
        ]

    async def test_restoration_can_be_turned_off(self) -> None:
        """For a deployment that wants the placeholder to reach the user."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        redactor = detector.redactor(restore_in_response=False)
        outcome = await redactor.redact_request([{"role": "user", "content": "Mario Rossi"}])
        placeholder = outcome.messages[0]["content"]
        assert (await redactor.restore_response(placeholder, outcome)).text == placeholder

    async def test_a_placeholder_split_across_frames_is_still_restored(self) -> None:
        """The reason TextRewriteStage exists. A placeholder arriving as
        `<PERS` / `ON_ABC` / `123>` is invisible to any per-frame rewriter."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        redactor = detector.redactor()
        outcome = await redactor.redact_request([{"role": "user", "content": "Mario Rossi"}])
        placeholder = outcome.messages[0]["content"]

        text = f"Hello {placeholder}, welcome."
        # One character per frame: the most hostile fragmentation possible.
        events = [
            SSEEvent.from_json({"choices": [{"index": 0, "delta": {"content": char}}]})
            for char in text
        ]
        events.append(
            SSEEvent.from_json({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        )
        events.append(SSEEvent.done())

        stage = redactor.response_stage(outcome)
        assert await collect(stage, events) == "Hello Mario Rossi, welcome."

    async def test_nothing_redacted_means_no_buffering_stage(self) -> None:
        detector = FakeDetector()
        redactor = detector.redactor()
        outcome = await redactor.redact_request([{"role": "user", "content": "hello"}])
        events = [
            SSEEvent.from_json({"choices": [{"index": 0, "delta": {"content": "hi"}}]}),
            SSEEvent.done(),
        ]
        assert await collect(redactor.response_stage(outcome), events) == "hi"

    async def test_an_unknown_placeholder_is_left_alone(self) -> None:
        """It may be something the user typed or the model invented; inventing an
        original for it would be worse than leaving it visible."""
        detector = FakeDetector({"Mario Rossi": "PERSON"})
        redactor = detector.redactor()
        outcome = await redactor.redact_request([{"role": "user", "content": "Mario Rossi"}])
        restored = (await redactor.restore_response("<PERSON_NEVERSEEN1>", outcome)).text
        assert restored == "<PERSON_NEVERSEEN1>"


async def collect(stage: Any, events: list[SSEEvent]) -> str:
    """Run *events* through *stage* and return the assistant text that came out."""

    async def source() -> Any:
        for event in events:
            yield event

    text = ""
    async for event in stage(source()):
        payload = event.json()
        if not isinstance(payload, dict):
            continue
        for choice in payload.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str):
                text += content
    return text
