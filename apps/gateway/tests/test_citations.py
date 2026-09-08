"""ADR 0059: a citation still points at the words it cites after redaction.

The bug this is all for: a provider returns web-search citations as character
offsets into the assistant's message, computed against the text *it* generated
— which contained our placeholders. Restoring `<PERSON_K3QF7RZM2A>` to a real
name of a different length moves every character after it, so an untouched
offset now points at the wrong words. No error, no log line; the citation just
quietly cites the wrong sentence.

Three things are pinned here, and the middle one is the reason the whole design
carries a choice index around:

* the arithmetic — where an offset lands after one, several, or overlapping
  substitutions;
* that each choice gets its **own** map, because `n: 2` is two different texts
  edited in two different places;
* that nothing moves when nothing was restored — which is the case for every
  deployment with redaction off, and the reason this was never a live problem.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import ApiSurface
from gateway.protocols import reader_for
from llmp_shared import PlaceholderMap, Restored

NAME = "Paolo Viviani"


def mapped(*pairs: tuple[str, str]) -> PlaceholderMap:
    placeholders = PlaceholderMap()
    for original, placeholder in pairs:
        placeholders.add("PERSON", original, placeholder)
    return placeholders


class TestTheArithmetic:
    def test_an_offset_after_a_longer_value_moves_right(self) -> None:
        restored = mapped((NAME, "<P_1>")).restore_with_edits("Hi <P_1>, see this.")
        assert restored.text == f"Hi {NAME}, see this."
        # "see" began at 10 in the original and at 10 + (13 - 5) in the answer.
        assert restored.shift(10) == 10 + len(NAME) - len("<P_1>")

    def test_an_offset_after_a_shorter_value_moves_left(self) -> None:
        restored = mapped(("Bo", "<PERSON_LONGISH>")).restore_with_edits("Hi <PERSON_LONGISH>!")
        assert restored.text == "Hi Bo!"
        assert restored.shift(19) == 5

    def test_an_offset_before_the_first_edit_does_not_move(self) -> None:
        restored = mapped((NAME, "<P_1>")).restore_with_edits("Hi <P_1>.")
        assert restored.shift(0) == 0
        assert restored.shift(3) == 3

    def test_several_edits_accumulate(self) -> None:
        restored = mapped((NAME, "<P_1>"), ("Bo", "<P_2>")).restore_with_edits(
            "<P_1> and <P_2> wrote it."
        )
        assert restored.text == f"{NAME} and Bo wrote it."
        # "wrote" was at 16; the first edit added 8, the second removed 3.
        assert restored.shift(16) == 16 + (len(NAME) - 5) + (len("Bo") - 5)

    def test_the_same_placeholder_twice_is_counted_twice(self) -> None:
        restored = mapped((NAME, "<P_1>")).restore_with_edits("<P_1> met <P_1> here.")
        assert restored.text == f"{NAME} met {NAME} here."
        assert len(restored.edits) == 2
        assert restored.shift(16) == 16 + 2 * (len(NAME) - 5)

    def test_a_placeholder_that_is_a_prefix_of_another_loses_to_it(self) -> None:
        """The property the old `str.replace` chain had, kept exactly.

        Substituting the short one first would rewrite the inside of the long
        one and leave a mangled placeholder nobody can restore.
        """
        placeholders = mapped(("short", "<P_1>"), ("long", "<P_1>X"))
        restored = placeholders.restore_with_edits("<P_1>X")
        assert restored.text == "long"
        assert [(e.at, e.was, e.now) for e in restored.edits] == [(0, 6, 4)]

    def test_an_offset_inside_a_replaced_run_lands_on_its_start(self) -> None:
        """No exact answer exists: the placeholder it pointed into is gone."""
        restored = mapped((NAME, "<PERSON_LONG_1>")).restore_with_edits("Hi <PERSON_LONG_1>.")
        assert restored.shift(8) == 3

    def test_nothing_mapped_moves_nothing(self) -> None:
        restored = PlaceholderMap().restore_with_edits("Untouched text.")
        assert restored.text == "Untouched text."
        assert restored.moved is False
        assert restored.shift(7) == 7

    def test_a_replacement_of_equal_length_moves_nothing(self) -> None:
        """`moved` is about lengths, not about whether text changed."""
        restored = mapped(("Bobbi", "<P_1>")).restore_with_edits("Hi <P_1>.")
        assert restored.text == "Hi Bobbi."
        assert restored.moved is False
        assert restored.shift(8) == 8

    def test_an_unknown_placeholder_is_not_an_edit(self) -> None:
        restored = mapped((NAME, "<P_1>")).restore_with_edits("<PERSON_NEVERSEEN> and <P_1>")
        assert "<PERSON_NEVERSEEN>" in restored.text
        assert len(restored.edits) == 1


class TestTheChatCompletionsShape:
    @staticmethod
    def body(content: str, *, start: int, end: int, index: int = 0) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "index": index,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url_citation": {
                                    "url": "https://example.org/a",
                                    "title": "A",
                                    "start_index": start,
                                    "end_index": end,
                                },
                            }
                        ],
                    },
                }
            ]
        }

    def test_the_cited_span_still_quotes_the_same_words(self) -> None:
        placeholders = mapped((NAME, "<P_1>"))
        original = "<P_1> won the match yesterday."
        cited = "the match"
        start = original.index(cited)
        payload = self.body(original, start=start, end=start + len(cited))

        restored = placeholders.restore_with_edits(original)
        payload["choices"][0]["message"]["content"] = restored.text
        moved = reader_for(ApiSurface.CHAT_COMPLETIONS).shift_citations(
            payload, lambda _choice, offset: restored.shift(offset)
        )

        assert moved is True
        citation = payload["choices"][0]["message"]["annotations"][0]["url_citation"]
        text = payload["choices"][0]["message"]["content"]
        assert text[citation["start_index"] : citation["end_index"]] == cited

    def test_each_choice_gets_its_own_map(self) -> None:
        """Two choices are two different texts, edited in two different places.

        One shared map would shift the second choice by the first choice's
        edits, which is the bug the choice index exists to prevent.
        """
        placeholders = mapped((NAME, "<P_1>"))
        first = "<P_1> won."
        second = "Yesterday <P_1> won."
        cited = "won"

        payload = {
            "choices": [
                self.body(first, start=first.index(cited), end=first.index(cited) + 3, index=0)[
                    "choices"
                ][0],
                self.body(
                    second, start=second.index(cited), end=second.index(cited) + 3, index=1
                )["choices"][0],
            ]
        }
        maps = {
            0: placeholders.restore_with_edits(first),
            1: placeholders.restore_with_edits(second),
        }
        for index, restored in maps.items():
            payload["choices"][index]["message"]["content"] = restored.text
        reader_for(ApiSurface.CHAT_COMPLETIONS).shift_citations(
            payload, lambda choice, offset: maps[choice].shift(offset)
        )

        for choice in payload["choices"]:
            citation = choice["message"]["annotations"][0]["url_citation"]
            content = choice["message"]["content"]
            assert content[citation["start_index"] : citation["end_index"]] == cited

    def test_a_streamed_delta_carries_them_too(self) -> None:
        payload = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url_citation": {"start_index": 10, "end_index": 20},
                            }
                        ]
                    },
                }
            ]
        }
        moved = reader_for(ApiSurface.CHAT_COMPLETIONS).shift_citations(
            payload, lambda _choice, offset: offset + 5
        )
        assert moved is True
        citation = payload["choices"][0]["delta"]["annotations"][0]["url_citation"]
        assert (citation["start_index"], citation["end_index"]) == (15, 25)

    def test_an_unchanged_offset_is_not_reported_as_moved(self) -> None:
        payload = self.body("text", start=0, end=4)
        assert (
            reader_for(ApiSurface.CHAT_COMPLETIONS).shift_citations(
                payload, lambda _choice, offset: offset
            )
            is False
        )


class TestTheResponsesShape:
    def test_offsets_on_an_output_item_are_moved(self) -> None:
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "restored",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "start_index": 3,
                                    "end_index": 8,
                                    "url": "https://example.org",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        moved = reader_for(ApiSurface.RESPONSES).shift_citations(
            payload, lambda _choice, offset: offset + 2
        )
        assert moved is True
        annotation = payload["output"][0]["content"][0]["annotations"][0]
        assert (annotation["start_index"], annotation["end_index"]) == (5, 10)

    def test_the_streamed_single_annotation_event_is_moved(self) -> None:
        payload = {
            "type": "response.output_text.annotation.added",
            "annotation": {"type": "url_citation", "start_index": 1, "end_index": 4},
        }
        assert (
            reader_for(ApiSurface.RESPONSES).shift_citations(
                payload, lambda _choice, offset: offset + 10
            )
            is True
        )
        assert payload["annotation"]["start_index"] == 11

    def test_the_completed_envelope_is_moved(self) -> None:
        payload = {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "annotations": [
                                    {"type": "url_citation", "start_index": 0, "end_index": 2}
                                ]
                            }
                        ],
                    }
                ]
            },
        }
        assert (
            reader_for(ApiSurface.RESPONSES).shift_citations(
                payload, lambda _choice, offset: offset + 1
            )
            is True
        )
        annotation = payload["response"]["output"][0]["content"][0]["annotations"][0]
        assert annotation["start_index"] == 1


class TestTheSurfacesWithNoOffsets:
    @pytest.mark.parametrize(
        "surface", [ApiSurface.MESSAGES, ApiSurface.IMAGES, ApiSurface.OCR]
    )
    def test_they_move_nothing(self, surface: ApiSurface) -> None:
        """Anthropic's citations quote their source rather than index ours.

        `cited_text` and `encrypted_index` travel with the citation, so
        rewriting our copy of the answer cannot invalidate them — and an image
        or a page has no citations at all.
        """
        payload = {"content": [{"type": "text", "text": "x", "citations": [{"cited_text": "y"}]}]}
        assert (
            reader_for(surface).shift_citations(payload, lambda _choice, offset: offset + 99)
            is False
        )


class TestThroughTheRoute:
    """End to end on `/v1/chat/completions`, which is where it would have bitten."""

    @staticmethod
    def searched(content: str, *, start: int, end: int) -> dict[str, Any]:
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "upstream/test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url_citation": {
                                    "url": "https://example.org/a",
                                    "title": "A",
                                    "start_index": start,
                                    "end_index": end,
                                },
                            }
                        ],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    async def test_a_citation_survives_restoration(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The whole point, through the real route with a real redactor.

        The upstream answers with a placeholder in the text and a citation
        pointing after it. The caller must get the real name *and* a citation
        that still quotes the same words.
        """
        from helpers import completion_body
        from llmp_shared import PLACEHOLDER_RE
        from test_redaction_http import FakeDetector

        detector = FakeDetector({NAME: "PERSON"})
        # No resolver, so the redactor's own policy is the effective one. With
        # a resolver present and no rules written, ADR 0039's default applies,
        # nothing is substituted, and this test would pass without testing
        # anything — the same trap test_ocr_surface.py documents.
        app.state.redaction = None
        app.state.redactor = detector.redactor()

        # One request first, purely to learn the placeholder this deployment's
        # key derives for the name. Predicting it here would duplicate the
        # derivation and stop testing it.
        fake_upstream.set_json(completion_body())
        await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": f"who is {NAME}?"}],
            },
            headers=seeded.auth,
        )
        upstream_saw = fake_upstream.bodies[-1]["messages"][-1]["content"]
        found = PLACEHOLDER_RE.search(upstream_saw)
        assert found, f"the request was not redacted: {upstream_saw!r}"
        placeholder = found.group(0)

        cited = "won the match"
        upstream_text = f"{placeholder} {cited} yesterday."
        start = upstream_text.index(cited)
        fake_upstream.set_json(self.searched(upstream_text, start=start, end=start + len(cited)))

        sent = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": f"did {NAME} win?"}],
            },
            headers=seeded.auth,
        )
        assert sent.status_code == 200
        message = sent.json()["choices"][0]["message"]
        assert message["content"].startswith(NAME), "the name was not restored"
        citation = message["annotations"][0]["url_citation"]
        assert (
            message["content"][citation["start_index"] : citation["end_index"]] == cited
        ), "the citation moved with the text"

    async def test_citations_are_untouched_when_nothing_is_redacted(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Redaction off — the default here — leaves the provider's offsets alone.

        Which is why this was never a live problem: with nothing substituted
        the response comes back byte for byte and the offsets still fit.
        """
        text = "Nothing sensitive won the match yesterday."
        cited = "won the match"
        start = text.index(cited)
        fake_upstream.set_json(self.searched(text, start=start, end=start + len(cited)))

        sent = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": "who won?"}],
            },
            headers=seeded.auth,
        )
        citation = sent.json()["choices"][0]["message"]["annotations"][0]["url_citation"]
        assert (citation["start_index"], citation["end_index"]) == (start, start + len(cited))


class TestTheStreamingRewriter:
    """A streamed citation's offsets are into the accumulated message.

    So the rewriter has to remember the provider's whole answer for the choice,
    which is what `_remember` and `_shift` are for.
    """

    async def test_a_citation_frame_is_shifted_by_the_text_before_it(self) -> None:
        from gateway.redaction.http import RestoreStage

        placeholders = mapped((NAME, "<P_1>"))
        stage = RestoreStage(placeholders, tail_size=0)

        # Two text frames, then a citation pointing into what was sent.
        arrived = "<P_1> won the match"
        cited = "won the match"
        start = arrived.index(cited)

        stage._remember(0, arrived)
        assert stage._shift(0, start) == start + len(NAME) - len("<P_1>")

    async def test_the_map_is_rebuilt_when_more_text_arrives(self) -> None:
        from gateway.redaction.http import RestoreStage

        stage = RestoreStage(mapped((NAME, "<P_1>")), tail_size=0)
        stage._remember(0, "<P_1> a")
        first = stage._shift(0, 6)
        stage._remember(0, "nd <P_1> b")
        second = stage._shift(0, 16)
        assert first == 6 + len(NAME) - 5
        assert second == 16 + 2 * (len(NAME) - 5)

    async def test_each_choice_is_remembered_separately(self) -> None:
        from gateway.redaction.http import RestoreStage

        stage = RestoreStage(mapped((NAME, "<P_1>")), tail_size=0)
        stage._remember(0, "<P_1> x")
        stage._remember(1, "no placeholder here")
        assert stage._shift(0, 6) == 6 + len(NAME) - 5
        assert stage._shift(1, 6) == 6

    async def test_a_rewriter_that_never_moves_anything_shifts_nothing(self) -> None:
        """The default `edits_in` returns None, and the offsets stay put."""
        from gateway.redaction.base import TextRewriteStage

        class Upper(TextRewriteStage):
            def transform(self, text: str, *, final: bool) -> str:
                return text.upper()

        stage = Upper(tail_size=0)
        stage._remember(0, "abc")
        assert stage._shift(0, 2) == 2

    def test_restored_is_what_the_stage_reports(self) -> None:
        from gateway.redaction.http import RestoreStage

        stage = RestoreStage(mapped((NAME, "<P_1>")), tail_size=0)
        assert isinstance(stage.edits_in("<P_1>"), Restored)
