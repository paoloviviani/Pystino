"""Same-length placeholders: ``#TOKEN#`` the width of the value it replaces.

Best effort, per entity type and per custom pattern: values under 8 characters,
and values whose stand-in would collide, get the tagged form.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson
from conftest import Seeded
from gateway.config import (
    CustomPattern,
    EntityMode,
    EntityPolicy,
    PlaceholderStyle,
    RedactionPolicy,
)
from gateway.redaction.note import SAME_LENGTH_NOTE, system_note
from helpers import completion_body
from test_redaction_http import FakeDetector
from test_redaction_restore_surfaces import CF, delta_chunk, frames, pieces, post_stream

SAME = PlaceholderStyle.SAME_LENGTH


def policy(mode: EntityMode = EntityMode.ANONYMISE_RESTORE, style: PlaceholderStyle = SAME) -> Any:
    return RedactionPolicy(
        default_mode=EntityMode.OFF,
        entities={"IT_FISCAL_CODE": EntityPolicy(mode=mode, placeholder=style)},
    )


def install(app: Any, detector: FakeDetector, **settings: Any) -> None:
    app.state.redactor = detector.redactor(**settings)
    app.state.redaction = None


class TestSubstitution:
    async def test_the_stand_in_is_as_long_as_the_value(self) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        outcome = await detector.redactor(policy=policy()).redact_request(
            [{"role": "user", "content": f"cf {CF} ok"}]
        )
        sent = outcome.messages[0]["content"]
        stand_in = sent.removeprefix("cf ").removesuffix(" ok")
        assert len(stand_in) == len(CF)
        assert stand_in.startswith("#") and stand_in.endswith("#")
        assert outcome.same_length_substitutions == 1
        assert outcome.placeholder_map.restore(sent) == f"cf {CF} ok"

    async def test_the_default_is_still_tagged(self) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        outcome = await detector.redactor(
            policy=policy(style=PlaceholderStyle.TAGGED)
        ).redact_request([{"role": "user", "content": CF}])
        assert outcome.messages[0]["content"].startswith("<IT_FISCAL_CODE_")
        assert outcome.same_length_substitutions == 0
        assert EntityPolicy().placeholder is PlaceholderStyle.TAGGED
        assert CustomPattern(name="x", regex="y").placeholder is PlaceholderStyle.TAGGED

    async def test_the_same_value_twice_is_one_stand_in_and_two_values_differ(self) -> None:
        other = "RSSMRA80A01F206Y"
        detector = FakeDetector({CF: "IT_FISCAL_CODE", other: "IT_FISCAL_CODE"})
        outcome = await detector.redactor(policy=policy()).redact_request(
            [
                {"role": "user", "content": f"{CF} {CF}"},
                {"role": "user", "content": other},
            ]
        )
        first, second = outcome.messages[0]["content"].split(" ")
        assert first == second
        assert outcome.messages[1]["content"] != first
        assert outcome.same_length_substitutions == 3

    async def test_a_short_value_falls_back_to_tagged(self) -> None:
        detector = FakeDetector({"AB12345": "IT_FISCAL_CODE"})
        outcome = await detector.redactor(policy=policy()).redact_request(
            [{"role": "user", "content": "AB12345"}]
        )
        assert outcome.messages[0]["content"].startswith("<IT_FISCAL_CODE_")
        assert outcome.same_length_substitutions == 0

    async def test_a_stand_in_already_in_the_text_falls_back_to_tagged(self) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        redactor = detector.redactor(policy=policy())
        first = await redactor.redact_request([{"role": "user", "content": CF}])
        stand_in = first.messages[0]["content"]
        # The user typed the exact stand-in beside the real value.
        outcome = await redactor.redact_request([{"role": "user", "content": f"{CF} {stand_in}"}])
        assert outcome.messages[0]["content"].startswith("<IT_FISCAL_CODE_")

    async def test_a_collision_with_another_value_falls_back_to_tagged(
        self, monkeypatch: Any
    ) -> None:
        import gateway.redaction.http as http

        monkeypatch.setattr(
            http, "same_length_placeholder", lambda *a, **k: "#COLLIDES" + "X" * 7 + "#"
        )
        other = "RSSMRA80A01F206Y"
        detector = FakeDetector({CF: "IT_FISCAL_CODE", other: "IT_FISCAL_CODE"})
        outcome = await detector.redactor(policy=policy()).redact_request(
            [{"role": "user", "content": f"{CF} {other}"}]
        )
        sent = outcome.messages[0]["content"]
        # Right to left: the later value claims the stand-in, the earlier one is tagged.
        assert sent.count("#COLLIDES") == 1
        assert "<IT_FISCAL_CODE_" in sent

    async def test_only_the_modes_that_write_a_token_use_it(self) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        redacted = await detector.redactor(policy=policy(EntityMode.REDACT)).redact_request(
            [{"role": "user", "content": CF}]
        )
        assert redacted.messages[0]["content"] == "<IT_FISCAL_CODE>"
        hidden = await detector.redactor(policy=policy(EntityMode.ANONYMISE)).redact_request(
            [{"role": "user", "content": CF}]
        )
        assert len(hidden.messages[0]["content"]) == len(CF)
        assert not hidden.placeholder_map

    async def test_a_custom_pattern_can_ask_for_it(self) -> None:
        detector = FakeDetector()
        custom = RedactionPolicy(
            patterns=[
                CustomPattern(
                    name="BADGE",
                    regex=r"BADGE-[0-9]{6}",
                    mode=EntityMode.ANONYMISE_RESTORE,
                    placeholder=SAME,
                )
            ]
        )
        outcome = await detector.redactor(policy=custom).redact_request(
            [{"role": "user", "content": "id BADGE-123456"}]
        )
        stand_in = outcome.messages[0]["content"].removeprefix("id ")
        assert len(stand_in) == len("BADGE-123456") and stand_in.startswith("#")

    async def test_stored_rules_without_the_field_load_as_tagged(self) -> None:
        loaded = RedactionPolicy.model_validate(
            {"entities": {"PERSON": {"mode": "redact"}}, "patterns": [{"name": "X", "regex": "y"}]}
        )
        assert loaded.placeholder_style_for("PERSON") is PlaceholderStyle.TAGGED
        assert loaded.placeholder_style_for("X") is PlaceholderStyle.TAGGED


class TestScopes:
    def test_any_scope_can_veto_none_can_impose(self) -> None:
        wants = policy(style=SAME)
        silent = policy(style=PlaceholderStyle.TAGGED)
        assert (
            RedactionPolicy.combine([wants, wants]).placeholder_style_for("IT_FISCAL_CODE") is SAME
        )
        folded = RedactionPolicy.combine([wants, silent])
        assert folded.placeholder_style_for("IT_FISCAL_CODE") is PlaceholderStyle.TAGGED

    def test_moving_to_same_length_counts_as_weakening(self) -> None:
        assert policy(style=SAME).weakens(policy(style=PlaceholderStyle.TAGGED))
        assert not policy(style=PlaceholderStyle.TAGGED).weakens(policy(style=SAME))


class TestOnTheWire:
    async def test_the_answer_comes_back_restored_streamed_and_whole(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        install(app, detector, policy=policy())
        outcome = await app.state.redactor.redact_request([{"role": "user", "content": CF}])
        stand_in = outcome.messages[0]["content"]
        request = {
            "model": seeded.model.name,
            "messages": [{"role": "user", "content": f"fill with {CF}"}],
        }

        fake_upstream.set_json(completion_body(f"Field: `{stand_in}`."))
        whole = await client.post("/v1/chat/completions", json=request, headers=seeded.auth)
        assert whole.json()["choices"][0]["message"]["content"] == f"Field: `{CF}`."
        sent = fake_upstream.last_body["messages"]
        assert CF not in orjson.dumps(sent).decode()
        assert stand_in in sent[-1]["content"]

        text = f"Field: `{stand_in}`."
        for size in (1, 4, 15, 16, 17, 40):
            chunks = [delta_chunk({"content": p}) for p in pieces(text, size)]
            chunks.append(delta_chunk({}, "stop"))
            fake_upstream.set_stream(frames(chunks))
            events = await post_stream(
                client, "/v1/chat/completions", {**request, "stream": True}, seeded
            )
            joined = "".join(
                c["delta"].get("content") or "" for e in events for c in e.get("choices") or []
            )
            assert joined == f"Field: `{CF}`.", size

    async def test_the_note_describes_the_stand_ins(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        install(app, detector, policy=policy())
        fake_upstream.set_json(completion_body("ok"))
        await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": CF}]},
            headers=seeded.auth,
        )
        system = fake_upstream.last_body["messages"][0]
        assert system["role"] == "system"
        assert SAME_LENGTH_NOTE in system["content"]

    async def test_a_tagged_policy_does_not_mention_them(self) -> None:
        note = system_note(policy(style=PlaceholderStyle.TAGGED))
        assert note is not None and "#TOKEN#" not in note
