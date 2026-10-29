"""The note that tells a model what a placeholder is.

Added only when a request was actually rewritten, in the surface's own system
prompt, never screened itself, and worded for whether the values come back.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.config import (
    CustomPattern,
    EntityMode,
    EntityPolicy,
    PlaceholderStyle,
    RedactionPolicy,
)
from gateway.models import ApiSurface
from gateway.redaction.base import RedactionOutcome
from gateway.redaction.note import (
    HIDDEN_NOTE,
    RESTORABLE_NOTE,
    SAME_LENGTH_NOTE,
    inject_system_note,
    system_note,
)
from helpers import completion_body
from test_redaction_http import FakeDetector
from test_redaction_restore_surfaces import CF


def _policy(mode: EntityMode, **kw: Any) -> RedactionPolicy:
    return RedactionPolicy(entities={"IT_FISCAL_CODE": EntityPolicy(mode=mode, **kw)})


RESTORABLE = system_note(_policy(EntityMode.ANONYMISE_RESTORE))
HIDDEN = system_note(_policy(EntityMode.ANONYMISE))
SURFACES = {
    "/v1/chat/completions": {"messages": [{"role": "user", "content": f"code {CF}"}]},
    "/v1/responses": {"input": f"code {CF}"},
    "/v1/messages": {"max_tokens": 10, "messages": [{"role": "user", "content": f"code {CF}"}]},
}


def install(app: Any, policy: RedactionPolicy | None = None, **settings: Any) -> FakeDetector:
    detector = FakeDetector({CF: "IT_FISCAL_CODE"})
    extra = {"policy": policy} if policy is not None else {}
    app.state.redactor = detector.redactor(**extra, **settings)
    app.state.redaction = None
    return detector


def upstream_reply(path: str) -> dict[str, Any]:
    if path == "/v1/chat/completions":
        return completion_body("ok")
    if path == "/v1/responses":
        return {"id": "r", "object": "response", "output": [], "usage": {}}
    return {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "content": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def system_text(path: str, sent: dict[str, Any]) -> str:
    """Every piece of system prompt the provider received, as one string."""
    if path == "/v1/responses":
        return str(sent.get("instructions") or "")
    if path == "/v1/messages":
        system = sent.get("system")
        if isinstance(system, list):
            return "\n".join(str(part.get("text")) for part in system)
        return str(system or "")
    return "\n".join(
        str(m["content"]) for m in sent["messages"] if m["role"] in ("system", "developer")
    )


async def call(
    client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any, path: str, **extra: Any
) -> dict[str, Any]:
    fake_upstream.set_json(upstream_reply(path))
    response = await client.post(
        path, json={"model": seeded.model.name, **SURFACES[path], **extra}, headers=seeded.auth
    )
    assert response.status_code == 200, response.text
    return fake_upstream.last_body  # type: ignore[no-any-return]


@pytest.mark.parametrize("path", list(SURFACES))
class TestPerSurface:
    async def test_a_restorable_substitution_adds_the_restorable_note(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        sent = await call(client, seeded, fake_upstream, path)
        assert RESTORABLE and RESTORABLE in system_text(path, sent)
        assert CF not in str(sent)

    async def test_an_anonymise_only_substitution_adds_the_hidden_note(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, RedactionPolicy(default_mode=EntityMode.ANONYMISE))
        sent = await call(client, seeded, fake_upstream, path)
        assert HIDDEN and HIDDEN in system_text(path, sent)
        assert RESTORABLE not in system_text(path, sent)

    async def test_restoring_switched_off_means_the_hidden_note(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, restore_in_response=False)
        sent = await call(client, seeded, fake_upstream, path)
        assert HIDDEN and HIDDEN in system_text(path, sent)

    async def test_a_rule_in_force_adds_the_note_even_when_nothing_matched(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        """The note follows the policy, so the system text is the same every turn."""
        detector = install(app)
        detector.spans_for.clear()
        sent = await call(client, seeded, fake_upstream, path)
        assert RESTORABLE and RESTORABLE in system_text(path, sent)

    async def test_two_turns_one_with_a_match_one_without_have_identical_system_text(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        detector = install(app)
        first = system_text(path, await call(client, seeded, fake_upstream, path))
        detector.spans_for.clear()
        later: dict[str, Any] = {
            "/v1/chat/completions": {"messages": [{"role": "user", "content": "thanks"}]},
            "/v1/responses": {"input": "thanks"},
            "/v1/messages": {"max_tokens": 10, "messages": [{"role": "user", "content": "thanks"}]},
        }
        fake_upstream.set_json(upstream_reply(path))
        await client.post(
            path, json={"model": seeded.model.name, **later[path]}, headers=seeded.auth
        )
        assert system_text(path, fake_upstream.last_body) == first != ""

    async def test_every_match_being_off_means_no_note(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        policy = RedactionPolicy(
            default_mode=EntityMode.OFF,
            entities={"IT_FISCAL_CODE": EntityPolicy(mode=EntityMode.OFF)},
        )
        install(app, policy)
        sent = await call(client, seeded, fake_upstream, path)
        assert system_text(path, sent) == ""
        assert CF in str(sent)

    async def test_redact_only_rules_get_the_hidden_note(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, RedactionPolicy(default_mode=EntityMode.REDACT))
        sent = await call(client, seeded, fake_upstream, path)
        assert HIDDEN and HIDDEN in system_text(path, sent)

    async def test_redaction_off_means_no_note(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        sent = await call(client, seeded, fake_upstream, path)
        assert system_text(path, sent) == ""

    async def test_the_note_is_added_to_the_callers_own_system_prompt(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        extra: dict[str, Any]
        if path == "/v1/chat/completions":
            extra = {
                "messages": [
                    {"role": "system", "content": "Be brief."},
                    {"role": "user", "content": f"code {CF}"},
                ]
            }
        elif path == "/v1/responses":
            extra = {"instructions": "Be brief."}
        else:
            extra = {"system": "Be brief."}
        sent = await call(client, seeded, fake_upstream, path, **extra)
        text = system_text(path, sent)
        assert text.startswith("Be brief.")
        assert RESTORABLE and RESTORABLE in text
        if path == "/v1/chat/completions":
            assert [m["role"] for m in sent["messages"]] == ["system", "user"]

    async def test_the_note_itself_is_never_redacted(
        self, path: str, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        detector = install(app)
        detector.spans_for["stand in for the user's real data"] = "PERSON"
        sent = await call(client, seeded, fake_upstream, path)
        assert RESTORABLE and RESTORABLE in system_text(path, sent)


class TestTheText:
    def test_the_two_variants_say_what_is_true_of_them(self) -> None:
        assert RESTORABLE and "put back automatically" in RESTORABLE
        assert "tool arguments" in RESTORABLE
        assert HIDDEN and "stay hidden" in HIDDEN
        assert "put back" not in HIDDEN

    def test_without_argument_restoration_it_does_not_promise_it(self) -> None:
        note = system_note(_policy(EntityMode.ANONYMISE_RESTORE), restore_arguments=False)
        assert note and "tool" not in note

    def test_restoring_off_or_no_restorable_entity_means_hidden(self) -> None:
        assert system_note(_policy(EntityMode.ANONYMISE_RESTORE), restore_text=False) == HIDDEN
        assert system_note(_policy(EntityMode.REDACT)) == HIDDEN

    def test_only_off_and_block_means_no_note(self) -> None:
        assert system_note(RedactionPolicy()) is None
        assert system_note(_policy(EntityMode.OFF)) is None
        assert system_note(_policy(EntityMode.BLOCK)) is None

    def test_a_custom_pattern_counts_as_a_rule_in_force(self) -> None:
        policy = RedactionPolicy(
            patterns=[CustomPattern(name="X", regex="y", mode=EntityMode.REDACT)]
        )
        assert system_note(policy) == HIDDEN

    def test_the_same_length_sentence_follows_the_policy_only(self) -> None:
        plain = system_note(_policy(EntityMode.ANONYMISE_RESTORE))
        same = system_note(
            _policy(EntityMode.ANONYMISE_RESTORE, placeholder=PlaceholderStyle.SAME_LENGTH)
        )
        assert same == f"{plain} {SAME_LENGTH_NOTE}"

    def test_the_wording_is_a_constant_and_short(self) -> None:
        for constant in (RESTORABLE_NOTE, HIDDEN_NOTE):
            assert constant.count(". ") <= 2

    def test_a_caller_message_list_is_not_mutated(self) -> None:
        messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
        payload = {"messages": messages}
        inject_system_note(
            payload,
            RedactionOutcome(messages=[], note="NOTE"),
            surface=ApiSurface.CHAT_COMPLETIONS,
        )
        assert messages[0]["content"] == "Be brief."
        assert payload["messages"][0]["content"] == "Be brief.\n\nNOTE"

    def test_the_callers_cached_anthropic_blocks_are_untouched(self) -> None:
        blocks = [
            {"type": "text", "text": "Long prefix", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "More"},
        ]
        payload: dict[str, Any] = {"system": [dict(b) for b in blocks]}
        inject_system_note(
            payload, RedactionOutcome(messages=[], note="NOTE"), surface=ApiSurface.MESSAGES
        )
        assert payload["system"][:2] == blocks
        assert payload["system"][2] == {"type": "text", "text": "NOTE"}
