"""The prompt estimate an abandoned stream is billed on (audit M8)."""

from __future__ import annotations

from gateway.accounting import DEFAULT_ESTIMATOR

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look a record up. " * 50,
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        },
    }
]


def test_tool_definitions_are_prompt_tokens() -> None:
    assert DEFAULT_ESTIMATOR.count_tools(TOOLS) > 200
    assert DEFAULT_ESTIMATOR.count_tools(None) == 0
    assert DEFAULT_ESTIMATOR.count_tools([]) == 0
    assert DEFAULT_ESTIMATOR.count_tools("not a list") == 0


def test_an_image_part_is_not_free() -> None:
    text_only = [{"role": "user", "content": [{"type": "text", "text": "what is this"}]}]
    with_image = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
    assert (
        DEFAULT_ESTIMATOR.count_messages(with_image)
        >= DEFAULT_ESTIMATOR.count_messages(text_only) + 1000
    )
