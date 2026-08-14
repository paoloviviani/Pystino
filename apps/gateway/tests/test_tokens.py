"""Local token estimation.

This is the fallback that runs when an upstream drops its usage frame. It is
approximate on purpose — there is no model-specific tokeniser — so the properties
worth asserting are structural: never zero for non-empty text, never crashes on
odd input, and roughly proportional to size.
"""

from __future__ import annotations

from gateway.accounting.tokens import DEFAULT_ESTIMATOR, HeuristicTokenEstimator


class TestCountText:
    def test_empty_text_is_zero(self) -> None:
        assert DEFAULT_ESTIMATOR.count_text("") == 0

    def test_non_empty_text_is_never_zero(self) -> None:
        """The entire point: never report zero for output that exists."""
        assert DEFAULT_ESTIMATOR.count_text("a") >= 1

    def test_latin_text_is_about_four_characters_per_token(self) -> None:
        text = "a" * 400
        assert 90 <= DEFAULT_ESTIMATOR.count_text(text) <= 110

    def test_cjk_is_counted_denser_than_latin(self) -> None:
        """chars/4 would understate Chinese by roughly a factor of four."""
        estimator = HeuristicTokenEstimator()
        cjk = estimator.count_text("日本語のテキストです")
        latin = estimator.count_text("a" * 10)
        assert cjk > latin
        assert cjk >= 10

    def test_longer_text_costs_more(self) -> None:
        short = DEFAULT_ESTIMATOR.count_text("hello")
        long = DEFAULT_ESTIMATOR.count_text("hello " * 100)
        assert long > short


class TestCountMessages:
    def test_includes_per_message_overhead(self) -> None:
        one = DEFAULT_ESTIMATOR.count_messages([{"role": "user", "content": "hi"}])
        two = DEFAULT_ESTIMATOR.count_messages(
            [{"role": "user", "content": "hi"}, {"role": "user", "content": "hi"}]
        )
        assert two > one

    def test_empty_conversation_still_has_priming_overhead(self) -> None:
        assert DEFAULT_ESTIMATOR.count_messages([]) > 0

    def test_multimodal_parts_array(self) -> None:
        estimator = HeuristicTokenEstimator()
        counted = estimator.count_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this " * 10},
                        {"type": "image_url", "image_url": {"url": "https://example/x.png"}},
                    ],
                }
            ]
        )
        assert counted > 20

    def test_tool_call_arguments_are_counted(self) -> None:
        estimator = HeuristicTokenEstimator()
        without = estimator.count_messages([{"role": "assistant", "content": ""}])
        with_tools = estimator.count_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "search",
                                "arguments": '{"query":"a long query string here"}',
                            },
                        }
                    ],
                }
            ]
        )
        assert with_tools > without

    def test_tolerates_missing_and_malformed_fields(self) -> None:
        """A provider-shaped message we do not recognise must not crash billing."""
        estimator = HeuristicTokenEstimator()
        assert estimator.count_messages([{}]) > 0
        assert estimator.count_messages([{"role": "user", "content": None}]) > 0
        assert estimator.count_messages([{"role": "user", "content": 12345}]) > 0
        assert estimator.count_messages([{"role": "user", "tool_calls": ["junk"]}]) > 0
        assert estimator.count_messages([{"content": [{"no_text": True}]}]) > 0
