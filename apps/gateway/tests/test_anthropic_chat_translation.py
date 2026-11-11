"""Chat completions to Anthropic Messages: the pure mapping.

Fixtures are shaped as Anthropic documents them
(https://platform.claude.com/docs/en/api/messages and the pages named in
``gateway.translate.anthropic_chat``), not as the translator happens to emit
them.
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar

import pytest
from gateway.accounting.cost import TokenCounts
from gateway.config import AnthropicSettings
from gateway.errors import BadRequestError
from gateway.translate.anthropic_chat import (
    ModelTraits,
    TranslationError,
    build_request,
    error_to_openai,
    message_to_chat_completion,
    reject_unsupported,
    thinking_record,
    usage_to_openai,
)

PNG = base64.b64encode(b"\x89PNG-bytes").decode()
PDF = base64.b64encode(b"%PDF-1.7 fake").decode()
LONG_SYSTEM = "You are a careful assistant. " * 50


def build(payload: dict[str, Any], *, max_output: int | None = None, **settings: Any) -> dict:
    payload = {"model": "claude-sonnet-4-5-20250929", **payload}
    request = build_request(
        payload, settings=AnthropicSettings(**settings), max_output_tokens=max_output
    )
    return request.body


def user(text: str = "hi") -> dict:
    return {"role": "user", "content": text}


class TestModelTraits:
    @pytest.mark.parametrize(
        ("model", "manual", "sampling", "forced"),
        [
            ("claude-haiku-4-5-20251001", True, True, True),
            ("claude-sonnet-4-5-20250929", True, True, True),
            ("claude-opus-4-20250514", True, True, True),
            ("claude-sonnet-4-6", False, True, True),
            ("claude-opus-4-7", False, False, True),
            ("claude-opus-5-5", False, False, False),
            ("claude-sonnet-5-5", False, False, False),
            ("claude-fable-5-1", False, False, False),
            ("claude-haiku-5-5", False, False, True),
            ("some-alias", False, False, True),
        ],
    )
    def test_generation_rules(self, model: str, manual: bool, sampling: bool, forced: bool) -> None:
        traits = ModelTraits.of(model)
        assert (traits.manual_thinking, traits.sampling_allowed, traits.forced_tools_allowed) == (
            manual,
            sampling,
            forced,
        )

    def test_a_dated_opus_4_is_not_read_as_minor_20(self) -> None:
        assert ModelTraits.of("claude-opus-4-20250514").version == (4, 0)


class TestMessages:
    def test_system_and_developer_become_top_level_blocks_in_order(self) -> None:
        body = build(
            {
                "messages": [
                    {"role": "system", "content": "one"},
                    {"role": "developer", "content": [{"type": "text", "text": "two"}]},
                    user(),
                ]
            },
            prompt_cache="off",
        )
        assert body["system"] == [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ]
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    def test_a_mid_conversation_system_message_is_a_user_note_not_system(self) -> None:
        body = build(
            {
                "messages": [
                    {"role": "system", "content": "top"},
                    user("q"),
                    {"role": "assistant", "content": "a"},
                    {"role": "system", "content": "new rule"},
                    user("q2"),
                ]
            },
            prompt_cache="off",
        )
        assert body["system"] == [{"type": "text", "text": "top"}]
        # merged into one user turn with the next user message
        assert body["messages"][2] == {
            "role": "user",
            "content": [
                {"type": "text", "text": "[system message]\nnew rule"},
                {"type": "text", "text": "q2"},
            ],
        }

    def test_consecutive_same_role_messages_merge(self) -> None:
        body = build({"messages": [user("a"), user("b")]}, prompt_cache="off")
        assert len(body["messages"]) == 1
        assert [b["text"] for b in body["messages"][0]["content"]] == ["a", "b"]

    def test_a_conversation_opening_with_assistant_gets_a_user_placeholder(self) -> None:
        body = build(
            {"messages": [{"role": "assistant", "content": "hello"}, user("go")]},
            prompt_cache="off",
        )
        assert body["messages"][0] == {
            "role": "user",
            "content": [{"type": "text", "text": "(conversation start)"}],
        }
        assert body["messages"][1]["role"] == "assistant"

    def test_images_data_url_and_http(self) -> None:
        body = build(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{PNG}",
                                    "detail": "auto",
                                },
                            },
                            {"type": "image_url", "image_url": {"url": "https://x.test/a.jpg"}},
                        ],
                    }
                ]
            },
            prompt_cache="off",
        )
        _, image, url = body["messages"][0]["content"]
        assert image == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": PNG},
        }
        assert url == {"type": "image", "source": {"type": "url", "url": "https://x.test/a.jpg"}}

    def test_an_unsupported_image_type_is_a_translation_error(self) -> None:
        with pytest.raises(TranslationError, match="JPEG, PNG, GIF or WebP"):
            build(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "data:image/tiff;base64,AAAA"},
                                }
                            ],
                        }
                    ]
                }
            )

    def test_pdf_and_text_files_become_documents(self) -> None:
        body = build(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "file",
                                "file": {
                                    "filename": "a.pdf",
                                    "file_data": f"data:application/pdf;base64,{PDF}",
                                },
                            },
                            {
                                "type": "file",
                                "file": {
                                    "filename": "n.txt",
                                    "file_data": "data:text/plain;base64,"
                                    + base64.b64encode("héllo".encode()).decode(),
                                },
                            },
                        ],
                    }
                ]
            },
            prompt_cache="off",
        )
        pdf, text = body["messages"][0]["content"]
        assert pdf == {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": PDF},
            "title": "a.pdf",
        }
        assert text["source"] == {"type": "text", "media_type": "text/plain", "data": "héllo"}

    def test_a_file_id_is_refused_by_name(self) -> None:
        with pytest.raises(TranslationError, match="file_id"):
            build(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "file", "file": {"file_id": "file-1"}}],
                        }
                    ]
                }
            )

    def test_tool_calls_and_results(self) -> None:
        body = build(
            {
                "messages": [
                    user("weather?"),
                    {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "ignored: no signature to send with it",
                        "tool_calls": [
                            {
                                "id": "call.1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"Rome"}'},
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "get_time", "arguments": "not json"},
                            },
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call.1", "content": "20C"},
                    {
                        "role": "tool",
                        "tool_call_id": "call_2",
                        "content": [
                            {"type": "text", "text": "noon"},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{PNG}"},
                            },
                        ],
                    },
                ]
            },
            prompt_cache="off",
        )
        assistant, results = body["messages"][1:]
        assert assistant["content"] == [
            {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Rome"}},
            {
                "type": "tool_use",
                "id": "call_2",
                "name": "get_time",
                "input": {"_invalid_arguments": "not json"},
            },
        ]
        # both results in ONE user turn, ids sanitised the same way
        assert results["role"] == "user"
        assert [b["tool_use_id"] for b in results["content"]] == ["call_1", "call_2"]
        assert results["content"][0]["content"] == [{"type": "text", "text": "20C"}]
        assert results["content"][1]["content"][1]["type"] == "image"

    def test_tool_results_precede_text_in_a_merged_user_turn(self) -> None:
        body = build(
            {
                "messages": [
                    user("q"),
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "t1",
                                "type": "function",
                                "function": {"name": "f", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "system", "content": "note"},
                    {"role": "tool", "tool_call_id": "t1", "content": "r"},
                ]
            },
            prompt_cache="off",
        )
        kinds = [b["type"] for b in body["messages"][2]["content"]]
        assert kinds == ["tool_result", "text"]


class TestToolsAndChoice:
    TOOL: ClassVar[Any] = {
        "type": "function",
        "function": {
            "name": "f",
            "description": "does f",
            "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
        },
    }

    def test_function_tools_become_input_schema_tools(self) -> None:
        body = build({"messages": [user()], "tools": [self.TOOL]}, prompt_cache="off")
        assert body["tools"] == [
            {
                "name": "f",
                "description": "does f",
                "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
            }
        ]
        assert "tool_choice" not in body

    @pytest.mark.parametrize(
        ("choice", "expected"),
        [
            ("none", {"type": "none"}),
            ("required", {"type": "any"}),
            ({"type": "function", "function": {"name": "f"}}, {"type": "tool", "name": "f"}),
        ],
    )
    def test_tool_choice(self, choice: Any, expected: dict) -> None:
        body = build(
            {"messages": [user()], "tools": [self.TOOL], "tool_choice": choice}, prompt_cache="off"
        )
        assert body["tool_choice"] == expected

    def test_parallel_tool_calls_false_disables_parallel_use(self) -> None:
        body = build(
            {
                "messages": [user()],
                "tools": [self.TOOL],
                "tool_choice": "required",
                "parallel_tool_calls": False,
            },
            prompt_cache="off",
        )
        assert body["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
        body = build(
            {"messages": [user()], "tools": [self.TOOL], "parallel_tool_calls": False},
            prompt_cache="off",
        )
        assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}

    def test_forced_tool_use_degrades_to_auto_where_the_model_refuses_it(self) -> None:
        body = build(
            {
                "model": "claude-opus-5-5",
                "messages": [user()],
                "tools": [self.TOOL],
                "tool_choice": "required",
            },
            prompt_cache="off",
        )
        assert "tool_choice" not in body

    def test_non_function_tools_are_refused_before_anything_is_spent(self) -> None:
        with pytest.raises(BadRequestError, match="function tools"):
            reject_unsupported({"tools": [{"type": "web_search_preview"}]})


class TestParameters:
    def test_max_tokens_precedence_and_fallbacks(self) -> None:
        assert build({"messages": [user()], "max_tokens": 10})["max_tokens"] == 10
        both = build({"messages": [user()], "max_tokens": 10, "max_completion_tokens": 20})
        assert both["max_tokens"] == 20
        # A model's whole output limit is not a default: capped, unless asked for.
        assert build({"messages": [user()]}, max_output=64000)["max_tokens"] == 32000
        assert build({"messages": [user()]}, max_output=4000)["max_tokens"] == 4000
        assert (
            build({"messages": [user()], "max_tokens": 64000}, max_output=64000)["max_tokens"]
            == 64000
        )
        assert build({"messages": [user()]})["max_tokens"] == 8192
        assert build({"messages": [user()]}, default_max_tokens=1000)["max_tokens"] == 1000

    def test_stop_user_and_stream(self) -> None:
        body = build(
            {"messages": [user()], "stop": "END", "user": "u-123", "stream": True},
            prompt_cache="off",
        )
        assert body["stop_sequences"] == ["END"]
        assert body["metadata"] == {"user_id": "u-123"}
        assert body["stream"] is True

    def test_an_email_user_is_hashed_not_forwarded(self) -> None:
        body = build({"messages": [user()], "user": "ann@example.org"}, prompt_cache="off")
        assert "@" not in body["metadata"]["user_id"]
        assert len(body["metadata"]["user_id"]) == 64

    def test_sampling_on_a_model_that_accepts_it(self) -> None:
        body = build({"messages": [user()], "temperature": 0.3, "top_p": 0.9}, prompt_cache="off")
        assert body["temperature"] == 0.3
        assert "top_p" not in body  # one of the two

    def test_sampling_is_dropped_where_the_model_refuses_it(self) -> None:
        for model in ("claude-opus-4-7", "claude-sonnet-5-5", "claude-haiku-5-5"):
            body = build(
                {"model": model, "messages": [user()], "temperature": 0.3, "top_p": 0.9},
                prompt_cache="off",
            )
            assert "temperature" not in body
            assert "top_p" not in body

    def test_penalties_and_seed_are_dropped(self) -> None:
        body = build(
            {
                "messages": [user()],
                "frequency_penalty": 0.5,
                "presence_penalty": 0.5,
                "seed": 7,
                "stream_options": {"include_usage": True},
            },
            prompt_cache="off",
        )
        assert set(body) == {"model", "max_tokens", "messages"}

    @pytest.mark.parametrize(
        "extra",
        [
            {"n": 2},
            {"logprobs": True},
            {"audio": {"voice": "alloy", "format": "wav"}},
            {"modalities": ["text", "audio"]},
            {"response_format": {"type": "json_object"}},
        ],
    )
    def test_unsupported_features_are_a_400(self, extra: dict) -> None:
        with pytest.raises(BadRequestError):
            reject_unsupported({"messages": [user()], **extra})

    def test_json_schema_becomes_output_config_format(self) -> None:
        body = build(
            {
                "messages": [user()],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "r",
                        "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
                    },
                },
            },
            prompt_cache="off",
        )
        assert body["output_config"] == {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        }


class TestReasoningEffort:
    def test_absent_means_no_thinking_parameters(self) -> None:
        body = build({"messages": [user()]}, prompt_cache="off")
        assert "thinking" not in body
        assert "output_config" not in body

    def test_manual_budget_on_the_4_5_generation(self) -> None:
        body = build(
            {
                "messages": [user()],
                "reasoning_effort": "medium",
                "max_tokens": 16000,
                "temperature": 0.2,
            },
            prompt_cache="off",
        )
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
        assert body["max_tokens"] > body["thinking"]["budget_tokens"]
        assert "temperature" not in body  # refused while thinking is on
        assert "output_config" not in body  # Sonnet 4.5 has no effort

    def test_budget_is_clamped_under_max_tokens_or_thinking_is_skipped(self) -> None:
        body = build(
            {"messages": [user()], "reasoning_effort": "high", "max_tokens": 4000},
            prompt_cache="off",
        )
        assert body["thinking"]["budget_tokens"] == 2000
        body = build(
            {"messages": [user()], "reasoning_effort": "high", "max_tokens": 1500},
            prompt_cache="off",
        )
        assert "thinking" not in body

    def test_adaptive_and_effort_on_current_models(self) -> None:
        body = build(
            {"model": "claude-opus-5-5", "messages": [user()], "reasoning_effort": "high"},
            prompt_cache="off",
        )
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert body["output_config"] == {"effort": "high"}

    def test_none_turns_thinking_off_only_where_the_api_allows_it(self) -> None:
        cases = {
            "claude-sonnet-5": {"type": "disabled"},
            "claude-sonnet-5-5": {"type": "between_tools"},
            "claude-haiku-5-5": {"type": "disabled"},
            "claude-opus-5-5": None,  # cannot be turned off: send nothing
            "claude-opus-4-8": None,  # already off by default
        }
        for model, expected in cases.items():
            body = build(
                {"model": model, "messages": [user()], "reasoning_effort": "none"},
                prompt_cache="off",
            )
            assert body.get("thinking") == expected, model

    def test_manual_thinking_is_dropped_when_tool_use_is_forced(self) -> None:
        tool = TestToolsAndChoice.TOOL
        body = build(
            {
                "messages": [user()],
                "tools": [tool],
                "tool_choice": "required",
                "reasoning_effort": "low",
            },
            prompt_cache="off",
        )
        assert "thinking" not in body
        assert body["tool_choice"] == {"type": "any"}

    def test_a_bad_value_is_a_400_with_the_parameter_named(self) -> None:
        with pytest.raises(TranslationError) as raised:
            build({"messages": [user()], "reasoning_effort": "loud"})
        assert raised.value.param == "reasoning_effort"


def tool_loop(*, thinking_first: bool = False) -> list[dict]:
    return [
        user("q"),
        {
            "role": "assistant",
            "content": "let me check",
            "tool_calls": [
                {"id": "toolu_1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                {"id": "toolu_2", "type": "function", "function": {"name": "g", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "r1"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "r2"},
    ]


THINKING = {"type": "thinking", "thinking": "hmm", "signature": "SIG1"}
REDACTED = {"type": "redacted_thinking", "data": "ENC"}


class TestThinkingReplay:
    def build_with(self, record: dict | None, **payload: Any) -> dict:
        request = build_request(
            {"model": "claude-opus-4-8", "messages": tool_loop(), **payload},
            settings=AnthropicSettings(prompt_cache="off"),
            max_output_tokens=None,
            records={1: record} if record else None,
        )
        return request.body

    def test_stored_blocks_are_replayed_verbatim_in_their_original_order(self) -> None:
        record = {
            "model": "claude-opus-4-8",
            "order": [THINKING, "text", REDACTED, "tool:toolu_1", "tool:toolu_2"],
        }
        assistant = self.build_with(record)["messages"][1]["content"]
        assert [b["type"] for b in assistant] == [
            "thinking",
            "text",
            "redacted_thinking",
            "tool_use",
            "tool_use",
        ]
        assert assistant[0] == THINKING
        assert assistant[2] == REDACTED

    def test_interleaved_order_is_kept(self) -> None:
        second = {"type": "thinking", "thinking": "again", "signature": "SIG2"}
        record = {
            "model": "claude-opus-4-8",
            "order": [THINKING, "tool:toolu_1", second, "tool:toolu_2"],
        }
        assistant = self.build_with(record)["messages"][1]["content"]
        # the message's text, which the record did not place, goes after the
        # leading thinking; the rest keeps the stored interleaving
        assert [b.get("id") or b["type"] for b in assistant] == [
            "thinking",
            "text",
            "toolu_1",
            "thinking",
            "toolu_2",
        ]
        assert assistant[3] == second

    def test_no_record_means_plain_blocks_and_no_error(self) -> None:
        assistant = self.build_with(None)["messages"][1]["content"]
        assert [b["type"] for b in assistant] == ["text", "tool_use", "tool_use"]

    def test_manual_thinking_without_the_stored_blocks_is_dropped_mid_loop(self) -> None:
        """Extended thinking requires the last assistant turn to open with a
        thinking block; without one the API answers 400, so thinking is off for
        that request (documented: expired blocks degrade, never fail)."""
        request = build_request(
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": tool_loop(),
                "tools": [TestToolsAndChoice.TOOL],
                "reasoning_effort": "medium",
                "max_tokens": 8000,
            },
            settings=AnthropicSettings(prompt_cache="off"),
            max_output_tokens=None,
        )
        assert "thinking" not in request.body
        with_blocks = build_request(
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": tool_loop(),
                "tools": [TestToolsAndChoice.TOOL],
                "reasoning_effort": "medium",
                "max_tokens": 8000,
            },
            settings=AnthropicSettings(prompt_cache="off"),
            max_output_tokens=None,
            records={
                1: {
                    "model": "claude-sonnet-4-5-20250929",
                    "order": [THINKING, "tool:toolu_1", "tool:toolu_2"],
                }
            },
        )
        assert with_blocks.body["thinking"]["type"] == "enabled"
        # interleaved thinking between tool calls needs the beta header on this generation
        assert with_blocks.headers == {"anthropic-beta": "interleaved-thinking-2025-05-14"}

    def test_adaptive_models_keep_thinking_on_and_just_omit_missing_blocks(self) -> None:
        body = self.build_with(None, reasoning_effort="low")
        assert body["thinking"]["type"] == "adaptive"

    def test_the_record_is_made_from_a_reply_with_thinking_and_a_tool_call_only(self) -> None:
        reply = [
            THINKING,
            {"type": "text", "text": "x"},
            {"type": "tool_use", "id": "toolu_9", "name": "f", "input": {}},
        ]
        record = thinking_record("m", reply)
        assert record == {
            "model": "m",
            "order": [THINKING, "text", "tool:toolu_9"],
            "tool_ids": ["toolu_9"],
        }
        assert thinking_record("m", [THINKING, {"type": "text", "text": "x"}]) is None
        assert (
            thinking_record("m", [{"type": "tool_use", "id": "t", "name": "f", "input": {}}])
            is None
        )


def cached(body: dict) -> list[str]:
    """Where the breakpoints are, as readable labels."""
    found = []
    for tool in body.get("tools") or []:
        if "cache_control" in tool:
            found.append(f"tool:{tool['name']}")
    for block in body.get("system") or []:
        if "cache_control" in block:
            found.append("system")
    for index, message in enumerate(body["messages"]):
        for block in message["content"]:
            if "cache_control" in block:
                found.append(f"message{index}:{block['type']}")
    return found


class TestPromptCache:
    TOOLS: ClassVar[Any] = [
        {"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "b", "parameters": {"type": "object"}}},
    ]

    def test_a_plain_chat_caches_the_system_and_the_newest_user_turn(self) -> None:
        body = build({"messages": [{"role": "system", "content": LONG_SYSTEM}, user("q")]})
        assert cached(body) == ["system", "message0:text"]
        assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_a_tool_loop_uses_exactly_four_breakpoints(self) -> None:
        messages = [
            {"role": "system", "content": LONG_SYSTEM},
            user("q"),
            *tool_loop()[1:],
        ]
        body = build({"messages": messages, "tools": self.TOOLS})
        labels = cached(body)
        assert len(labels) == 4
        assert labels == ["tool:b", "system", "message0:text", "message2:tool_result"]

    def test_never_more_than_four_however_long_the_conversation(self) -> None:
        messages: list[dict] = [{"role": "system", "content": "s"}]
        for n in range(10):
            messages += [user(f"q{n}"), {"role": "assistant", "content": f"a{n}"}]
        messages.append(user("last"))
        assert len(cached(build({"messages": messages, "tools": self.TOOLS}))) <= 4

    def test_the_breakpoint_goes_on_the_tool_result_not_on_a_thinking_block(self) -> None:
        body = build({"messages": tool_loop()})
        assert "message2:tool_result" in cached(body)

    def test_client_breakpoints_are_kept_and_count_against_the_four(self) -> None:
        marked = {
            "type": "text",
            "text": "big doc",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
        body = build(
            {
                "messages": [
                    {"role": "system", "content": [marked]},
                    user("q1"),
                    {"role": "assistant", "content": "a"},
                    user("q2"),
                ],
                "tools": self.TOOLS,
            },
            prompt_cache_ttl="1h",
        )
        assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert len(cached(body)) == 4
        # ours carry no ttl when the client wrote some: a 1h entry must come first
        auto = [
            b["cache_control"]
            for m in body["messages"]
            for b in m["content"]
            if "cache_control" in b
        ]
        assert all("ttl" not in c for c in auto)

    def test_four_client_breakpoints_leave_no_room(self) -> None:
        part = lambda t: {"type": "text", "text": t, "cache_control": {"type": "ephemeral"}}  # noqa: E731
        body = build(
            {
                "messages": [
                    {"role": "user", "content": [part("a"), part("b"), part("c"), part("d")]}
                ]
            }
        )
        assert len(cached(body)) == 4

    def test_off_adds_nothing_and_keeps_the_clients(self) -> None:
        body = build(
            {"messages": [{"role": "system", "content": "s"}, user("q")]}, prompt_cache="off"
        )
        assert cached(body) == []
        body = build(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
                        ],
                    }
                ]
            },
            prompt_cache="off",
        )
        assert cached(body) == ["message0:text"]

    def test_one_hour_ttl_when_configured(self) -> None:
        body = build(
            {"messages": [{"role": "system", "content": "s"}, user("q")]}, prompt_cache_ttl="1h"
        )
        assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def breakpoints(body: dict) -> list[tuple[str, dict]]:
    """Every ``cache_control`` in the body, in the order Anthropic reads the prompt.

    Tools, then system, then each message's blocks. A ``tool_result`` is listed
    before the blocks nested in its ``content``, which is where they sit in the
    prompt. Each item is ``(label, control)``.
    """
    found: list[tuple[str, dict]] = []
    for tool in body.get("tools") or []:
        if "cache_control" in tool:
            found.append((f"tool:{tool['name']}", tool["cache_control"]))
    for block in body.get("system") or []:
        if "cache_control" in block:
            found.append(("system", block["cache_control"]))
    for index, message in enumerate(body["messages"]):
        for block in message["content"]:
            if "cache_control" in block:
                found.append((f"message{index}:{block['type']}", block["cache_control"]))
            if isinstance(block.get("content"), list):
                for inner in block["content"]:
                    if "cache_control" in inner:
                        found.append(
                            (
                                f"message{index}:{block['type']}>{inner['type']}",
                                inner["cache_control"],
                            )
                        )
    return found


def ttl_of(control: dict) -> str:
    return str(control.get("ttl", "5m"))


def longest_first(body: dict) -> bool:
    """The ordering Anthropic documents: a 1-hour entry must come before any
    5-minute one (https://platform.claude.com/docs/en/build-with-claude/prompt-caching,
    "Mixing different TTLs")."""
    ttls = [ttl_of(control) for _, control in breakpoints(body)]
    return ttls == sorted(ttls, key=lambda ttl: ttl != "1h")


MARK = {"type": "ephemeral"}
HOUR = {"type": "ephemeral", "ttl": "1h"}


class TestClientCacheControl:
    """A ``cache_control`` the client wrote is carried to the right block, counted
    against Anthropic's four, and never doubled by an automatic one."""

    TOOLS: ClassVar[Any] = [
        {"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "b", "parameters": {"type": "object"}}},
    ]

    def test_a_mark_on_a_tool_is_carried_and_the_last_tool_still_gets_ours(self) -> None:
        tools = [
            {**self.TOOLS[0], "cache_control": dict(MARK)},
            self.TOOLS[1],
        ]
        body = build({"messages": [user("q")], "tools": tools})
        assert body["tools"][0]["cache_control"] == MARK
        assert [label for label, _ in breakpoints(body)] == ["tool:a", "tool:b", "message0:text"]

    def test_a_mark_inside_the_function_object_is_read_too(self) -> None:
        tools = [
            self.TOOLS[0],
            {"type": "function", "function": {**self.TOOLS[1]["function"], "cache_control": HOUR}},
        ]
        body = build({"messages": [user("q")], "tools": tools})
        assert body["tools"][1]["cache_control"] == HOUR
        assert "cache_control" not in body["tools"][1].get("input_schema", {})

    def test_a_mark_on_the_last_tool_is_not_doubled(self) -> None:
        tools = [self.TOOLS[0], {**self.TOOLS[1], "cache_control": dict(MARK)}]
        body = build({"messages": [user("q")], "tools": tools})
        assert [label for label, _ in breakpoints(body)].count("tool:b") == 1
        assert len(breakpoints(body)) == 2  # the tool, and the newest user turn

    @pytest.mark.parametrize(
        "url",
        [f"data:image/png;base64,{PNG}", "https://example.org/cat.png"],
        ids=["data url", "http url"],
    )
    def test_a_mark_on_an_image_part_lands_on_the_image_block(self, url: str) -> None:
        body = build(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is this"},
                            {"type": "image_url", "image_url": {"url": url}, "cache_control": HOUR},
                        ],
                    }
                ]
            }
        )
        blocks = body["messages"][0]["content"]
        assert blocks[1]["type"] == "image"
        assert blocks[1]["cache_control"] == HOUR
        assert "cache_control" not in blocks[0]
        # the image is already the newest turn's last cacheable block: ours reuses it
        assert len(breakpoints(body)) == 1

    def test_a_mark_on_a_file_part_lands_on_the_document_block(self) -> None:
        body = build(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "file",
                                "file": {"file_data": PDF, "filename": "a.pdf"},
                                "cache_control": dict(MARK),
                            },
                            {"type": "text", "text": "summarise"},
                        ],
                    }
                ]
            }
        )
        blocks = body["messages"][0]["content"]
        assert blocks[0]["type"] == "document"
        assert blocks[0]["cache_control"] == MARK
        # ours goes on the last block, so the document and the question are both marked
        assert [label for label, _ in breakpoints(body)] == ["message0:document", "message0:text"]

    def test_a_mark_on_a_tool_message_lands_on_its_tool_result(self) -> None:
        messages = [
            user("q"),
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "big", "cache_control": HOUR},
        ]
        body = build({"messages": messages})
        result = body["messages"][2]["content"][0]
        assert result["type"] == "tool_result"
        assert result["cache_control"] == HOUR
        assert len(breakpoints(body)) == 2  # the tool_result (not doubled) and the first turn

    def test_a_mark_on_a_part_of_a_tool_result_counts_against_the_four(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            user("q"),
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": [{"type": "text", "text": "big", "cache_control": dict(MARK)}],
            },
        ]
        body = build({"messages": messages, "tools": self.TOOLS})
        found = [label for label, _ in breakpoints(body)]
        assert "message2:tool_result>text" in found  # the client's, nested, kept as written
        assert len(found) == 4  # one client + three of ours, never five

    def test_four_marks_inside_tool_results_leave_no_room(self) -> None:
        def result(call: str) -> dict:
            return {
                "role": "tool",
                "tool_call_id": call,
                "content": [{"type": "text", "text": call, "cache_control": dict(MARK)}],
            }

        calls = [
            {"id": f"t{n}", "type": "function", "function": {"name": "a", "arguments": "{}"}}
            for n in range(4)
        ]
        messages = [
            {"role": "system", "content": "s"},
            user("q"),
            {"role": "assistant", "content": None, "tool_calls": calls},
            *(result(f"t{n}") for n in range(4)),
        ]
        body = build({"messages": messages, "tools": self.TOOLS})
        assert len(breakpoints(body)) == 4
        assert all(">" in label for label, _ in breakpoints(body))

    def test_the_callers_payload_is_not_changed_by_ours(self) -> None:
        import copy

        payload = {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [{"role": "system", "content": LONG_SYSTEM}, user("q")],
            "tools": [{**self.TOOLS[0], "cache_control": dict(MARK)}, self.TOOLS[1]],
        }
        before = copy.deepcopy(payload)
        build_request(payload, settings=AnthropicSettings(), max_output_tokens=None)
        assert payload == before


class TestMixedTtl:
    """Anthropic: "a 1-hour cache entry must appear before any 5-minute cache entries".

    Our breakpoints are 5-minute when the client wrote any, so one the client put
    on a late block as 1-hour would sit after our 5-minute ones on the tools and
    the system prompt. They take the hour too: a request that carries a 1-hour
    breakpoint bills everything up to its last one as 1-hour writes, so it costs
    nothing more and is the only way to keep those breakpoints and the order.
    """

    TOOLS: ClassVar[Any] = TestPromptCache.TOOLS

    def history(self, *, mark: str) -> dict:
        """A tool loop with a system prompt and tools; *mark* names where the
        client put its one 1-hour breakpoint: ``newest``, ``first``, ``system``
        or ``tool``."""
        control = dict(HOUR)
        first: Any = user("q1")
        newest: Any = {"role": "tool", "tool_call_id": "t1", "content": "r"}
        system: Any = {"role": "system", "content": LONG_SYSTEM}
        tools = [dict(t) for t in self.TOOLS]
        if mark == "newest":
            newest["cache_control"] = control
        elif mark == "first":
            first = {
                "role": "user",
                "content": [{"type": "text", "text": "q1", "cache_control": control}],
            }
        elif mark == "system":
            system = {
                "role": "system",
                "content": [{"type": "text", "text": LONG_SYSTEM, "cache_control": control}],
            }
        elif mark == "tool":
            tools[0]["cache_control"] = control
        messages = [
            system,
            first,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}}
                ],
            },
            newest,
        ]
        return build({"messages": messages, "tools": tools})

    def test_a_late_one_hour_mark_does_not_follow_our_five_minute_ones(self) -> None:
        body = self.history(mark="newest")
        found = breakpoints(body)
        assert longest_first(body), found
        # the client's own mark is untouched, and every breakpoint is still there
        assert dict(body["messages"][2]["content"][0]["cache_control"]) == HOUR
        assert [label for label, _ in found] == [
            "tool:b",
            "system",
            "message0:text",
            "message2:tool_result",
        ]

    def test_a_one_hour_mark_on_the_first_turn_pulls_the_tools_and_system_up_with_it(self) -> None:
        body = self.history(mark="first")
        assert longest_first(body), breakpoints(body)
        by_label = {label: ttl_of(c) for label, c in breakpoints(body)}
        assert by_label["tool:b"] == by_label["system"] == by_label["message0:text"] == "1h"
        # after the last 1-hour mark, ours stay the default
        assert by_label["message2:tool_result"] == "5m"

    def test_ours_stay_five_minutes_when_no_one_hour_mark_follows_them(self) -> None:
        tool = {label: ttl_of(c) for label, c in breakpoints(self.history(mark="tool"))}
        # the client's hour is on the first tool: everything of ours is after it
        assert tool == {
            "tool:a": "1h",
            "tool:b": "5m",
            "system": "5m",
            "message2:tool_result": "5m",
        }  # the first turn has no slot: one client mark and three of ours make the four

    def test_a_one_hour_mark_on_the_system_prompt_lifts_the_last_tool_only(self) -> None:
        system = {label: ttl_of(c) for label, c in breakpoints(self.history(mark="system"))}
        assert system == {
            "tool:b": "1h",
            "system": "1h",
            "message0:text": "5m",
            "message2:tool_result": "5m",
        }

    def test_a_client_five_minute_mark_never_makes_ours_longer(self) -> None:
        body = build(
            {
                "messages": [
                    {"role": "system", "content": LONG_SYSTEM},
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "q", "cache_control": dict(MARK)}],
                    },
                ],
                "tools": self.TOOLS,
            },
            prompt_cache_ttl="1h",
        )
        assert {ttl_of(c) for _, c in breakpoints(body)} == {"5m"}

    def test_the_configured_hour_still_applies_when_the_client_wrote_nothing(self) -> None:
        body = self.history_without_marks()
        assert {ttl_of(c) for _, c in breakpoints(body)} == {"1h"}

    def history_without_marks(self) -> dict:
        return build(
            {
                "messages": [{"role": "system", "content": LONG_SYSTEM}, *tool_loop()],
                "tools": self.TOOLS,
            },
            prompt_cache_ttl="1h",
        )

    @pytest.mark.parametrize("mark", ["newest", "first", "system", "tool"])
    def test_whatever_the_position_the_prompt_is_in_the_documented_order(self, mark: str) -> None:
        body = self.history(mark=mark)
        assert longest_first(body), breakpoints(body)
        assert len(breakpoints(body)) <= 4


class TestResponse:
    def test_text_reply(self) -> None:
        message = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5-20250929",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        out = message_to_chat_completion(message, client_model="claude")
        assert out["object"] == "chat.completion"
        assert out["model"] == "claude"
        assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hello!"}
        assert out["choices"][0]["finish_reason"] == "stop"

    def test_thinking_text_and_tool_calls(self) -> None:
        message = {
            "id": "msg_02",
            "content": [
                {"type": "thinking", "thinking": "plan", "signature": "S"},
                {"type": "redacted_thinking", "data": "E"},
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "Rome"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        choice = message_to_chat_completion(message, client_model="m")["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] == "Checking."
        assert choice["message"]["reasoning_content"] == "plan"
        assert choice["message"]["tool_calls"] == [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Rome"}'},
            }
        ]

    @pytest.mark.parametrize(
        ("stop", "finish"),
        [
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("max_tokens", "length"),
            ("model_context_window_exceeded", "length"),
            ("tool_use", "tool_calls"),
            ("refusal", "content_filter"),
            ("pause_turn", "stop"),
            ("something_new", "stop"),
        ],
    )
    def test_finish_reasons(self, stop: str, finish: str) -> None:
        out = message_to_chat_completion({"content": [], "stop_reason": stop}, client_model="m")
        assert out["choices"][0]["finish_reason"] == finish


class TestUsageAndLedger:
    USAGE: ClassVar[Any] = {
        "input_tokens": 50,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 1000,
        "output_tokens": 30,
        "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 0},
    }

    def test_prompt_tokens_include_cache_reads_and_writes(self) -> None:
        usage = usage_to_openai(self.USAGE)
        assert usage["prompt_tokens"] == 1250
        assert usage["completion_tokens"] == 30
        assert usage["total_tokens"] == 1280
        assert usage["prompt_tokens_details"] == {"cached_tokens": 1000, "cache_write_tokens": 200}

    def test_the_ledger_reader_gets_the_same_slices_as_the_messages_reader(self) -> None:
        """The translated usage, read the OpenAI way, must equal Anthropic's own
        usage read the Anthropic way. If the two readers ever disagreed one
        would double count or drop the cache slices (AGENTS.md)."""
        via_chat = TokenCounts.from_usage(usage_to_openai(self.USAGE))
        native = TokenCounts.from_anthropic_usage(self.USAGE)
        assert via_chat == native
        assert (via_chat.prompt, via_chat.cached_prompt, via_chat.cache_write) == (1250, 1000, 200)
        assert via_chat.billable_prompt == 50

    def test_thinking_tokens_are_reported_as_reasoning_tokens(self) -> None:
        usage = usage_to_openai(
            {"input_tokens": 1, "output_tokens": 9, "output_tokens_details": {"thinking_tokens": 4}}
        )
        assert usage["completion_tokens_details"] == {"reasoning_tokens": 4}


class TestErrors:
    def test_shape_and_type_mapping(self) -> None:
        status, body = error_to_openai(
            400,
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "max_tokens: Field required"},
                "request_id": "req_1",
            },
        )
        assert status == 400
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "invalid_request_error"
        assert "max_tokens: Field required" in body["error"]["message"]
        assert "req_1" in body["error"]["message"]

    def test_overloaded_529_becomes_503_with_a_retry_hint(self) -> None:
        status, body = error_to_openai(
            529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        )
        assert status == 503
        assert body["error"]["type"] == "server_error"
        assert "retry" in body["error"]["message"].lower()

    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, "authentication_error"),
            (403, "permission_error"),
            (429, "rate_limit_error"),
            (500, "api_error"),
        ],
    )
    def test_status_is_preserved(self, status: int, kind: str) -> None:
        got, body = error_to_openai(
            status, {"type": "error", "error": {"type": kind, "message": "m"}}
        )
        assert got == status
        assert body["error"]["code"] == kind

    def test_a_non_json_error_still_yields_a_valid_body(self) -> None:
        status, body = error_to_openai(502, None)
        assert status == 502
        assert body["error"]["message"]
