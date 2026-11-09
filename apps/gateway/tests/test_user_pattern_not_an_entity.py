"""A user's custom pattern is evaluated in the gateway, never asked of the service.

`UserRule.as_policy()` used to list the pattern's name under `entities` too, so
`EffectivePolicy.asks()` sent it to the redaction service as an NER entity type. The
service answered "cannot serve": a warning on every request, and a 503 wherever the
detection is strict (the user's test box).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from conftest import Seeded
from gateway.config import (
    Contributor,
    EffectivePolicy,
    EntityMode,
    EntityPolicy,
    KeywordList,
    NerAsk,
    RedactionPolicy,
)
from gateway.config import CustomPattern as Pattern
from gateway.privacy.finds import find_in
from gateway.redaction import user_rules
from gateway.redaction.user_rules import UserRule
from privacy_helpers import add_pipeline, add_user_policy
from test_admin import as_user
from test_privacy_ner_engines import FakeService

M = EntityMode
TEXT = "ticket PRJ-42 for Mario Rossi"
KNOWN = {"IBAN_CODE", "EMAIL_ADDRESS", "CREDIT_CARD", "PERSON", "LOCATION", "ORGANIZATION"}


class StrictAboutTypes(FakeService):
    """Answers, like the real service, with the requested types its engine cannot serve."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        if request.url.path == "/detect" and response.status_code == 200:
            body = response.json()
            wanted = json_types(request)
            body["unsupported_types"] = sorted(t for t in wanted if t not in KNOWN)
            return httpx.Response(200, json=body)
        return response


def json_types(request: httpx.Request) -> list[str]:
    import json

    return list(json.loads(request.content).get("entity_types") or [])


def pattern_rule() -> UserRule:
    return UserRule(uuid.uuid4(), "pattern", "LIVE_PROJECT", "PRJ-[0-9]+", M.REDACT)


class TestAsks:
    def test_a_user_pattern_is_not_an_entity_type(self) -> None:
        policy = pattern_rule().as_policy()
        assert policy.entities == {} and policy.formats == {}
        assert [p.name for p in policy.patterns] == ["LIVE_PROJECT"]

    def test_a_user_mode_for_a_type_still_is(self) -> None:
        rule = UserRule(uuid.uuid4(), "mode", "PERSON", None, M.REDACT)
        assert list(rule.as_policy().entities) == ["PERSON"]

    def test_the_user_pattern_is_not_in_what_is_asked_of_the_service(self) -> None:
        rule = pattern_rule()
        effective = EffectivePolicy(
            policy=user_rules.fold(RedactionPolicy(), [rule]), contributors=(rule.contributor(),)
        )
        assert effective.asks() == [NerAsk(None, (), ())]

    def test_a_name_that_is_a_pattern_or_a_keyword_list_anywhere_is_never_asked(self) -> None:
        # Whatever a contributor lists under `entities`, if another contributor (or the same
        # one) defines that name as a pattern or a keyword list, it is evaluated here.
        a = RedactionPolicy(
            entities={"LIVE_PROJECT": EntityPolicy(mode=M.REDACT), "PERSON": EntityPolicy()}
        )
        b = RedactionPolicy(patterns=[Pattern(name="LIVE_PROJECT", regex="PRJ-[0-9]+")])
        c = RedactionPolicy(entities={"CODES": EntityPolicy(mode=M.REDACT)})
        d = RedactionPolicy(keywords=[KeywordList(name="CODES", words=["Falcon"])])
        effective = EffectivePolicy(
            policy=RedactionPolicy.combine([a, b, c, d]),
            contributors=tuple(
                Contributor(id=uuid.uuid4(), name=n, owner="admin", policy=p)
                for n, p in (("A", a), ("B", b), ("C", c), ("D", d))
            ),
        )
        (ask,) = effective.asks()  # one call, the default engine
        assert ask.types == ("PERSON",)


class TestStrictDetection:
    async def test_finds_on_a_user_policy_with_a_pattern_counts_it(self, caplog: Any) -> None:
        service = StrictAboutTypes()
        policy = user_rules.fold(RedactionPolicy(), [pattern_rule()])
        with caplog.at_level(logging.WARNING):
            result = await find_in(
                policy,
                [TEXT],
                redactor=service.redactor(),
                default_threshold=0.5,
                user_patterns=frozenset({("LIVE_PROJECT", "PRJ-[0-9]+")}),
            )
        assert result.unavailable is None
        assert [i.type for i in result.items] == ["LIVE_PROJECT"]
        assert "cannot serve" not in caplog.text

    async def test_the_users_test_box_answers_200_not_503(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        caplog: Any,
    ) -> None:
        service = StrictAboutTypes()
        app.state.settings.redaction.engine = "http"
        app.state.settings.redaction.endpoint = "http://detector:8080"
        app.state.control_http = service.client()
        app.state.redactor = service.redactor()
        app.state.redaction = None
        policy_id = await add_user_policy(
            app,
            session_factory,
            seeded.user.id,
            [{"kind": "pattern", "name": "LIVE_PROJECT", "regex": "PRJ-[0-9]+", "mode": "redact"}],
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "Mine",
            owner=seeded.user.id,
            who={"mode": "users", "ids": [str(seeded.user.id)]},
            otherwise={"level": "pass", "redact_with": str(policy_id)},
        )
        as_user(app, seeded.user)
        with caplog.at_level(logging.WARNING):
            result = await client.post(
                "/api/me/privacy/test", json={"kind": "chat", "text": "ticket PRJ-42"}
            )
        assert result.status_code == 200, result.text
        assert "cannot serve" not in caplog.text
        assert all("LIVE_PROJECT" not in json_types_of(r) for r in service.requests)


def json_types_of(payload: dict[str, Any]) -> list[str]:
    return list(payload.get("entity_types") or [])
