"""The system note under the v2 rules model: it follows the effective policy.

``PrivacyState.effective_policy`` stamps each hiding rule's mode onto its policies, and the
note is computed from that folded policy. A rule with no condition always hides, so its note is
there on every turn; a rule behind a condition ("finds >= 2") hides, and so adds the note,
only on the turns where the condition holds (the note is then a function of the rules in
force, which is the existing behaviour, reported rather than changed).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
from conftest import Seeded
from gateway.config import EntityPolicy, PlaceholderStyle, RedactionPolicy
from gateway.models import PrivacyPipeline
from gateway.redaction.note import SAME_LENGTH_NOTE
from helpers import completion_body
from privacy_helpers import add_policy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_redaction_http import FakeDetector
from test_redaction_restore_surfaces import CF

OTHER = "BNCLRA70A41F205Z"


async def _noop() -> None:
    return None


async def install(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
    conditions: list[dict[str, Any]],
    detector: FakeDetector,
) -> None:
    policy_id = await add_policy(
        app,
        session_factory,
        "Codes",
        entities={
            "IT_FISCAL_CODE": EntityPolicy(placeholder=PlaceholderStyle.SAME_LENGTH),
        },
    )
    rule = {
        "id": str(uuid.uuid4()),
        "name": "hide",
        "enabled": True,
        "policies": [str(policy_id)],
        "match": "all",
        "conditions": conditions,
        "mode": "anonymise_restore",
        "overrides": {},
    }
    async with session_factory() as db:
        db.add(
            PrivacyPipeline(
                owner_id=None,
                kind="content",
                name="Hide codes",
                enabled=True,
                position=0,
                who={"mode": "all", "ids": []},
                target={"mode": "all", "providers": [], "models": [], "model_types": ["chat"]},
                rules=[rule],
            )
        )
        await db.commit()
    await app.state.privacy.refresh()
    app.state.redactor = detector.redactor()
    # The request path takes the rules' effective policy only when a resolver is mounted; the
    # environment's own policy ships filtering nothing (ADR 0039).
    app.state.redaction = SimpleNamespace(
        policy=RedactionPolicy(), redactor=app.state.redactor, aclose=_noop
    )


async def system_of(
    client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any, text: str
) -> str:
    fake_upstream.set_json(completion_body("ok"))
    response = await client.post(
        "/v1/chat/completions",
        json={"model": seeded.model.name, "messages": [{"role": "user", "content": text}]},
        headers=seeded.auth,
    )
    assert response.status_code == 200, response.text
    return "\n".join(
        str(m["content"])
        for m in fake_upstream.last_body["messages"]
        if m["role"] in ("system", "developer")
    )


class TestUnconditionalRule:
    async def test_the_same_length_sentence_is_on_every_turn(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: Any,
    ) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        await install(app, session_factory, [], detector)
        with_match = await system_of(client, seeded, fake_upstream, f"code {CF}")
        detector.spans_for.clear()
        without = await system_of(client, seeded, fake_upstream, "thanks")
        assert SAME_LENGTH_NOTE in with_match
        assert with_match == without


class TestConditionalRule:
    async def test_a_finds_condition_adds_the_note_only_on_matching_turns(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: Any,
    ) -> None:
        detector = FakeDetector({CF: "IT_FISCAL_CODE", OTHER: "IT_FISCAL_CODE"})
        await install(app, session_factory, [{"key": "finds", "values": [], "count": 2}], detector)
        one = await system_of(client, seeded, fake_upstream, f"code {CF}")
        two = await system_of(client, seeded, fake_upstream, f"code {CF} and {OTHER}")
        assert one == ""
        assert SAME_LENGTH_NOTE in two
