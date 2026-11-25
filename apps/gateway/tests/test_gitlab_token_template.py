"""The console's GITLAB_TOKEN template, evaluated by the gateway's own engine (RE2).

The expression is read from the console's catalogue, so the test cannot drift from what
users are offered, and it runs through ``pattern_spans``, the path a request takes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from gateway.config import CustomPattern, EntityMode, RedactionPolicy
from gateway.redaction.http import pattern_spans

_SOURCE = Path(__file__).resolve().parents[2] / "console/src/lib/redactionTemplates.ts"
BODY = "x" * 20


def _template_regex() -> str:
    found = re.search(
        r'name: "GITLAB_TOKEN",.*?regex: ("(?:[^"\\]|\\.)*"),', _SOURCE.read_text(), re.S
    )
    assert found, "GITLAB_TOKEN template not found"
    return str(json.loads(found.group(1)))


def _matched(text: str) -> list[str]:
    policy = RedactionPolicy(
        patterns=[
            CustomPattern(name="GITLAB_TOKEN", regex=_template_regex(), mode=EntityMode.BLOCK)
        ]
    )
    return [text[s.start : s.end] for s in pattern_spans(text, policy)]


def test_routable_token_is_matched_whole() -> None:
    token = f"glpat-{BODY}.01.0y11nprdd"
    assert _matched(f"use {token} now") == [token]


def test_legacy_token_is_matched() -> None:
    assert _matched(f"glpat-{BODY}") == [f"glpat-{BODY}"]


@pytest.mark.parametrize("tail", [" .", ".", "...", ".\nNext line", ". Next"])
def test_trailing_period_is_not_swallowed(tail: str) -> None:
    token = f"glpat-{BODY}.01.0y11nprdd"
    assert _matched(f"token {token}{tail}") == [token]


@pytest.mark.parametrize(
    "prefix",
    ["gldt", "glrt", "glptt", "glft", "glimt", "gloas", "glcbt", "glsoat", "glffct", "glagent"],
)
def test_other_gitlab_prefixes_are_matched(prefix: str) -> None:
    assert _matched(f"{prefix}-{BODY}") == [f"{prefix}-{BODY}"]
