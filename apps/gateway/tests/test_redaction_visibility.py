"""Redaction visibility for users (ADR 0112).

Four things, each with a property worth pinning:

* the **fold**: a user's rules apply last and can only tighten (and say so when
  they do not), and apply to that user alone;
* the **log**: what was hidden and which rule decided, and never a value;
* the **management API**: validation exactly as an administrator's patterns get,
  nothing that loosens can even be written, nobody reads another user's rules;
* the **per-response summary**: opt-in, byte-for-byte unchanged without it, and
  the originals only with two headers and a user token, never stored.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import orjson
import pytest
from conftest import Seeded
from gateway.config import (
    CustomPattern,
    EntityMode,
    EntityPolicy,
    RedactionPolicy,
    RedactionSettings,
)
from gateway.deps import get_management_user
from gateway.models import (
    Base,
    RedactionRule,
    RedactionScope,
    UsageRecord,
    UsageStatus,
    User,
    UserRedactionRule,
)
from gateway.redaction.resolver import RedactionResolver, _user_rules_of
from gateway.redaction.user_rules import UserRule, fold, tightens, validate_pattern
from helpers import chunk, completion_body, sse, usage_only_frame
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_bearer_auth import auth, make_token
from test_redaction_http import FakeDetector

SECRET_NAME = "Mario Rossi"


def policy(**kwargs: Any) -> RedactionPolicy:
    return RedactionPolicy(**kwargs)


def mode_rule(name: str, mode: EntityMode) -> UserRule:
    return UserRule(id=uuid.uuid4(), kind="mode", name=name, regex=None, mode=mode)


def pattern_rule(name: str, regex: str, mode: EntityMode = EntityMode.REDACT) -> UserRule:
    return UserRule(id=uuid.uuid4(), kind="pattern", name=name, regex=regex, mode=mode)


def row(user_id: uuid.UUID, rule: UserRule) -> UserRedactionRule:
    return UserRedactionRule(
        id=rule.id,
        user_id=user_id,
        kind=rule.kind,
        name=rule.name,
        regex=rule.regex,
        mode=rule.mode.value,
    )


def admin_rule(
    scope: RedactionScope, subject: uuid.UUID | None, name: str, **kw: Any
) -> RedactionRule:
    return RedactionRule(
        id=uuid.uuid4(),
        name=name,
        scope=scope,
        scope_id=subject,
        policy=policy(**kw).model_dump(mode="json"),
    )


def resolver_with(
    admin: list[RedactionRule] | None = None, users: list[UserRedactionRule] | None = None
) -> RedactionResolver:
    resolver = RedactionResolver(RedactionSettings(), lambda: None)  # type: ignore[arg-type]
    resolver._reload_rules(admin or [])
    resolver._reload_user_rules(users or [])
    return resolver


class TestTheFold:
    """A user's rules fold in last, and only ever tighten."""

    def test_a_user_adds_a_pattern(self) -> None:
        base = policy(entities={"PERSON": EntityPolicy()})
        folded = fold(base, [pattern_rule("PROJECT", r"PRJ-\d+", EntityMode.BLOCK)])

        assert {p.name for p in folded.patterns} == {"PROJECT"}
        assert folded.mode_for("PROJECT") is EntityMode.BLOCK
        # What the administrators set is untouched.
        assert folded.mode_for("PERSON") is EntityMode.ANONYMISE_RESTORE

    def test_a_user_tightens_a_mode(self) -> None:
        base = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)})
        folded = fold(base, [mode_rule("PERSON", EntityMode.REDACT)])

        assert folded.mode_for("PERSON") is EntityMode.REDACT

    def test_a_user_can_protect_a_type_the_administrators_never_named(self) -> None:
        folded = fold(RedactionPolicy(), [mode_rule("EMAIL_ADDRESS", EntityMode.ANONYMISE)])

        assert folded.mode_for("EMAIL_ADDRESS") is EntityMode.ANONYMISE
        assert folded.detected_types() == ["EMAIL_ADDRESS"]

    def test_a_user_cannot_loosen_a_mode(self) -> None:
        base = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})
        weaker = mode_rule("PERSON", EntityMode.ANONYMISE_RESTORE)

        assert fold(base, [weaker]).mode_for("PERSON") is EntityMode.REDACT
        assert tightens(base, weaker) is False

    def test_an_equal_mode_changes_nothing_and_says_so(self) -> None:
        base = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})
        same = mode_rule("PERSON", EntityMode.REDACT)

        assert tightens(base, same) is False

    def test_an_admin_block_stays_a_block(self) -> None:
        base = policy(entities={"US_SSN": EntityPolicy(mode=EntityMode.BLOCK)})
        folded = fold(base, [mode_rule("US_SSN", EntityMode.ANONYMISE)])

        assert folded.mode_for("US_SSN") is EntityMode.BLOCK

    def test_the_administrators_default_is_a_floor_too(self) -> None:
        base = policy(default_mode=EntityMode.REDACT)

        assert (
            fold(base, [mode_rule("PERSON", EntityMode.ANONYMISE)]).mode_for("PERSON")
            is EntityMode.REDACT
        )

    def test_it_never_touches_what_only_an_administrator_may_set(self) -> None:
        """Thresholds, the allow-list and same-length stand-ins stay as they were."""
        from gateway.config import PlaceholderStyle

        base = policy(
            default_mode=EntityMode.OFF,
            entities={
                "PERSON": EntityPolicy(
                    mode=EntityMode.ANONYMISE_RESTORE,
                    threshold=0.8,
                    placeholder=PlaceholderStyle.SAME_LENGTH,
                )
            },
            allow_list=["acme.test"],
        )
        folded = fold(base, [mode_rule("PERSON", EntityMode.REDACT)])

        assert folded.allow_list == ["acme.test"]
        assert folded.threshold_for("PERSON", 0.0) == 0.8
        assert folded.placeholder_style_for("PERSON") is PlaceholderStyle.SAME_LENGTH

    def test_a_pattern_named_like_an_admin_pattern_cannot_loosen_it(self) -> None:
        base = policy(patterns=[CustomPattern(name="PROJECT", regex=r"PRJ-\d+")])
        weaker = pattern_rule("PROJECT", r"PRJ-\d+", EntityMode.ANONYMISE_RESTORE)

        folded = fold(base, [weaker])

        assert folded.mode_for("PROJECT") is EntityMode.REDACT
        assert len(folded.patterns) == 1

    def test_a_pattern_named_like_an_admin_type_cannot_loosen_it(self) -> None:
        base = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)})
        folded = fold(base, [pattern_rule("PERSON", r"Mario", EntityMode.ANONYMISE_RESTORE)])

        assert folded.mode_for("PERSON") is EntityMode.BLOCK

    def test_off_is_not_a_choice(self) -> None:
        with pytest.raises(ValueError, match="cannot be 'off'"):
            validate_pattern("X", r"abc", EntityMode.OFF)

    def test_a_pattern_gets_the_administrators_validation(self) -> None:
        with pytest.raises(ValueError, match="cannot be used"):
            validate_pattern("X", r"(?=lookahead)abc", EntityMode.REDACT)
        with pytest.raises(ValueError, match="empty string"):
            validate_pattern("X", r"a*", EntityMode.REDACT)


class TestResolution:
    """Whose rules apply, and who gets the blame."""

    def test_a_user_pattern_applies_only_to_that_user(self) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        resolver = resolver_with(users=[row(mine, pattern_rule("PROJECT", r"PRJ-\d+"))])

        assert [p.name for p in resolver.policy_for(user_id=mine).policy.patterns] == ["PROJECT"]
        assert resolver.policy_for(user_id=theirs).policy.patterns == []

    def test_the_user_rule_takes_the_blame_only_where_it_tightened(self) -> None:
        user_id = uuid.uuid4()
        catch_all = admin_rule(
            RedactionScope.ALL,
            None,
            "Everything",
            entities={
                "PERSON": EntityPolicy(mode=EntityMode.REDACT),
                "URL": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE),
            },
        )
        resolver = resolver_with(
            [catch_all],
            [
                row(user_id, mode_rule("PERSON", EntityMode.ANONYMISE)),  # weaker than redact
                row(user_id, mode_rule("URL", EntityMode.BLOCK)),  # stricter
            ],
        )
        effective = resolver.policy_for(user_id=user_id)

        person = effective.decider_for("PERSON")
        url = effective.decider_for("URL")
        assert person is not None and person.owner == "admin" and person.id == catch_all.id
        assert person.name == "Everything"
        assert url is not None and url.owner == "user" and url.name == "URL"
        assert effective.policy.mode_for("PERSON") is EntityMode.REDACT
        assert effective.policy.mode_for("URL") is EntityMode.BLOCK

    def test_the_administrators_answer_is_unchanged_for_a_user_with_no_rules(self) -> None:
        user_id = uuid.uuid4()
        catch_all = admin_rule(
            RedactionScope.ALL, None, "Everything", entities={"PERSON": EntityPolicy()}
        )
        resolver = resolver_with(
            [catch_all], [row(uuid.uuid4(), mode_rule("PERSON", EntityMode.BLOCK))]
        )

        admin = resolver.admin_policy_for(user_id=user_id)
        assert resolver.policy_for(user_id=user_id) == admin

    def test_the_fold_is_memoised_and_cleared_when_the_rules_change(self) -> None:
        user_id = uuid.uuid4()
        resolver = resolver_with(users=[row(user_id, pattern_rule("A", r"aaa"))])

        first = resolver.policy_for(user_id=user_id).policy
        assert resolver.policy_for(user_id=user_id).policy is first

        resolver.replace_user_rules(user_id, [row(user_id, pattern_rule("B", r"bbb"))])
        assert [p.name for p in resolver.policy_for(user_id=user_id).policy.patterns] == ["B"]

    def test_an_unreadable_row_is_dropped_not_fatal(self) -> None:
        good = uuid.uuid4()
        parsed = _user_rules_of(
            [
                UserRedactionRule(
                    user_id=uuid.uuid4(), kind="pattern", name="X", regex="(?=no)", mode="redact"
                ),
                UserRedactionRule(
                    user_id=uuid.uuid4(), kind="mode", name="PERSON", regex=None, mode="off"
                ),
                row(good, mode_rule("PERSON", EntityMode.REDACT)),
            ]
        )

        assert list(parsed) == [good]


# ---------------------------------------------------------------------------
# through the application
# ---------------------------------------------------------------------------


def as_user(app: Any, user: User) -> None:
    app.dependency_overrides[get_management_user] = lambda: user


def install(
    app: Any,
    *,
    admin: list[RedactionRule] | None = None,
    detects: dict[str, str] | None = None,
) -> RedactionResolver:
    """A detector that finds what it is told to, and a resolver holding these rules."""
    detector = FakeDetector(detects if detects is not None else {SECRET_NAME: "PERSON"})
    app.state.redactor = detector.redactor()
    resolver = resolver_with(admin)
    app.state.redaction = resolver
    return resolver


def catch_all(**entities: EntityPolicy) -> RedactionRule:
    return admin_rule(RedactionScope.ALL, None, "Cloud providers", entities=entities)


def body_for(seeded: Seeded, content: str = f"hello {SECRET_NAME}") -> dict[str, Any]:
    return {"model": seeded.model.name, "messages": [{"role": "user", "content": content}]}


SUMMARY = {"x-pystino-redaction-summary": "1"}
BOTH = {**SUMMARY, "x-pystino-redaction-values": "1"}


class TestTheManagementApi:
    async def test_a_user_creates_lists_and_deletes_a_rule(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE))])
        as_user(app, seeded.user)

        created = await client.post(
            "/api/me/redaction/rules",
            json={"kind": "mode", "name": "person", "mode": "redact"},
        )
        assert created.status_code == 201, created.text
        rule = created.json()
        assert rule["name"] == "PERSON"
        assert rule["status"] == "adds_protection"
        assert rule["admin_mode"] == "anonymise_restore"

        listing = (await client.get("/api/me/redaction/rules")).json()
        assert [item["id"] for item in listing["items"]] == [rule["id"]]
        assert listing["total"] == 1

        gone = await client.delete(f"/api/me/redaction/rules/{rule['id']}")
        assert gone.status_code == 204
        assert (await client.get("/api/me/redaction/rules")).json()["items"] == []

    async def test_a_weaker_rule_is_saved_and_reported_as_covered(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.BLOCK))])
        as_user(app, seeded.user)

        created = await client.post(
            "/api/me/redaction/rules",
            json={"kind": "mode", "name": "PERSON", "mode": "anonymise"},
        )

        assert created.status_code == 201
        assert created.json()["status"] == "covered"
        assert created.json()["admin_mode"] == "block"

    @pytest.mark.parametrize(
        "extra",
        [
            {"mode": "off"},
            {"threshold": 0.1},
            {"allow_list": ["x"]},
            {"placeholder": "same_length"},
        ],
    )
    async def test_nothing_that_loosens_can_be_written(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, extra: dict[str, Any]
    ) -> None:
        install(app)
        as_user(app, seeded.user)

        response = await client.post(
            "/api/me/redaction/rules",
            json={"kind": "mode", "name": "PERSON", "mode": "redact", **extra},
        )

        assert response.status_code == 400, response.text

    async def test_a_pattern_is_validated_as_an_administrators_is(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app)
        as_user(app, seeded.user)

        lookahead = await client.post(
            "/api/me/redaction/rules",
            json={"kind": "pattern", "name": "P", "regex": "(?=x)x", "mode": "redact"},
        )
        empty = await client.post(
            "/api/me/redaction/rules",
            json={"kind": "pattern", "name": "P", "regex": "a*", "mode": "redact"},
        )

        assert lookahead.status_code == 400
        assert "cannot be used" in lookahead.json()["error"]["message"]
        assert empty.status_code == 400
        assert "empty string" in empty.json()["error"]["message"]

    async def test_a_second_rule_of_the_same_name_is_a_conflict(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app)
        as_user(app, seeded.user)
        body = {"kind": "mode", "name": "PERSON", "mode": "redact"}

        assert (await client.post("/api/me/redaction/rules", json=body)).status_code == 201
        assert (await client.post("/api/me/redaction/rules", json=body)).status_code == 409

    async def test_the_number_of_rules_is_bounded(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        from gateway.redaction.user_rules import MAX_RULES_PER_USER

        install(app)
        as_user(app, seeded.user)
        for index in range(MAX_RULES_PER_USER):
            created = await client.post(
                "/api/me/redaction/rules",
                json={"kind": "mode", "name": f"TYPE_{index}", "mode": "redact"},
            )
            assert created.status_code == 201

        over = await client.post(
            "/api/me/redaction/rules", json={"kind": "mode", "name": "ONE_MORE", "mode": "redact"}
        )
        assert over.status_code == 400
        assert over.json()["error"]["code"] == "too_many_rules"

    async def test_nobody_reads_or_changes_another_users_rules(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        install(app)
        async with session_factory() as db:
            other = User(issuer="https://idp.test", subject="other", email="other@example.org")
            db.add(other)
            await db.flush()
            theirs = row(other.id, mode_rule("PERSON", EntityMode.BLOCK))
            db.add(theirs)
            await db.commit()
        as_user(app, seeded.user)

        assert (await client.get("/api/me/redaction/rules")).json()["items"] == []
        assert (await client.delete(f"/api/me/redaction/rules/{theirs.id}")).status_code == 404
        assert (
            await client.put(
                f"/api/me/redaction/rules/{theirs.id}",
                json={"kind": "mode", "name": "PERSON", "mode": "redact"},
            )
        ).status_code == 404

    async def test_the_rules_in_force_are_the_administrators_in_plain_terms(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app)
        as_user(app, seeded.user)
        async with app.state.session_factory() as db:
            db.add(
                RedactionRule(
                    id=uuid.uuid4(),
                    name="Cloud providers",
                    scope=RedactionScope.ALL,
                    scope_id=None,
                    policy=policy(
                        entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)},
                        patterns=[CustomPattern(name="SECRET_PROJECT", regex=r"zeus-\d+")],
                        allow_list=["acme.test"],
                    ).model_dump(mode="json"),
                )
            )
            await db.commit()

        body = (await client.get("/api/me/redaction/in-force")).json()

        assert body["rules"][0]["name"] == "Cloud providers"
        assert body["rules"][0]["entities"] == [
            {"entity_type": "PERSON", "mode": "anonymise_restore"}
        ]
        assert body["rules"][0]["patterns"] == [{"name": "SECRET_PROJECT", "mode": "redact"}]
        assert body["rules"][0]["exceptions"] == 1
        # The regex of an administrator's pattern and the allow-list's values
        # are not shown to users.
        assert "zeus" not in orjson.dumps(body).decode()
        assert "acme.test" not in orjson.dumps(body).decode()


class TestTheTestBox:
    async def test_it_names_what_would_be_hidden_and_which_rule_decided(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE))])
        as_user(app, seeded.user)
        await client.post(
            "/api/me/redaction/rules",
            json={"kind": "pattern", "name": "PROJECT", "regex": r"PRJ-\d+", "mode": "redact"},
        )
        text_ = f"{SECRET_NAME} is on PRJ-42"

        response = await client.post("/api/me/redaction/test", json={"text": text_})

        assert response.status_code == 200, response.text
        found = {span["entity_type"]: span for span in response.json()["spans"]}
        assert found["PERSON"]["rule"]["owner"] == "admin"
        assert found["PERSON"]["rule"]["name"] == "Cloud providers"
        assert found["PROJECT"]["rule"]["owner"] == "user"
        assert found["PROJECT"]["mode"] == "redact"
        assert (found["PROJECT"]["start"], found["PROJECT"]["end"]) == (
            text_.index("PRJ-42"),
            text_.index("PRJ-42") + 6,
        )
        # Offsets and types: the text is not echoed back.
        assert SECRET_NAME not in response.text
        assert "PRJ-42" not in response.text

    async def test_the_text_is_never_logged(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        as_user(app, seeded.user)

        with caplog.at_level(logging.DEBUG):
            await client.post("/api/me/redaction/test", json={"text": f"{SECRET_NAME} here"})

        assert SECRET_NAME not in caplog.text

    async def test_a_block_is_reported_not_refused(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.BLOCK))])
        as_user(app, seeded.user)

        response = await client.post("/api/me/redaction/test", json={"text": f"{SECRET_NAME} here"})

        assert response.status_code == 200
        assert response.json()["blocked"] is True

    async def test_with_the_engine_off_it_says_nothing_is_hidden(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)

        body = (await client.post("/api/me/redaction/test", json={"text": "x"})).json()

        assert body["engine_active"] is False
        assert body["spans"] == []


class TestOnTheRequestPath:
    async def test_a_user_pattern_redacts_that_users_requests_and_only_theirs(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        resolver = install(app, detects={})
        as_user(app, seeded.user)
        # An administrator's rule makes redaction active; the user's pattern adds to it.
        resolver._reload_rules([catch_all(PERSON=EntityPolicy())])
        created = await client.post(
            "/api/me/redaction/rules",
            json={"kind": "pattern", "name": "PROJECT", "regex": r"PRJ-\d+", "mode": "redact"},
        )
        assert created.status_code == 201
        fake_upstream.set_json(completion_body())

        mine = await client.post(
            "/v1/chat/completions", json=body_for(seeded, "see PRJ-42"), headers=seeded.auth
        )
        assert mine.status_code == 200, mine.text
        assert "PRJ-42" not in orjson.dumps(fake_upstream.bodies[-1]).decode()
        assert "<PROJECT>" in orjson.dumps(fake_upstream.bodies[-1]).decode()

        # Another user's request through the same gateway is not touched.
        async with session_factory() as db:
            other = User(
                issuer="https://idp.test",
                subject="other",
                email="other@example.org",
                default_billing_group_id=seeded.group.id,
            )
            db.add(other)
            await db.commit()
        assert resolver.policy_for(user_id=other.id).policy.patterns == []

    async def test_a_user_tightening_applies_and_a_weaker_rule_does_not(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.REDACT))])
        as_user(app, seeded.user)
        await client.post(
            "/api/me/redaction/rules",
            json={"kind": "mode", "name": "PERSON", "mode": "anonymise_restore"},
        )
        fake_upstream.set_json(completion_body())

        await client.post("/v1/chat/completions", json=body_for(seeded), headers=seeded.auth)

        sent = orjson.dumps(fake_upstream.bodies[-1]).decode()
        assert "<PERSON>" in sent  # the administrators' redact, not the user's weaker mode
        assert SECRET_NAME not in sent

    async def test_an_admin_block_stays_a_block_against_a_user_rule(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.BLOCK))])
        as_user(app, seeded.user)
        await client.post(
            "/api/me/redaction/rules", json={"kind": "mode", "name": "PERSON", "mode": "redact"}
        )
        before = len(fake_upstream.bodies)

        response = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers=seeded.auth
        )

        assert response.status_code == 403
        assert len(fake_upstream.bodies) == before


class TestTheLog:
    async def test_a_request_leaves_an_entry_with_the_rule_and_no_value(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE))])
        as_user(app, seeded.user)
        fake_upstream.set_json(completion_body())

        sent = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [
                    {"role": "user", "content": f"hello {SECRET_NAME}"},
                    {"role": "tool", "tool_call_id": "t1", "content": f"{SECRET_NAME} again"},
                ],
            },
            headers={**seeded.auth, "x-request-id": "req-log-1"},
        )
        assert sent.status_code == 200

        events = (await client.get("/api/me/redaction/events")).json()
        assert events["total"] == 1
        item = events["items"][0]
        assert item["request_id"] == "req-log-1"
        assert item["model"] == "test-model"
        assert item["provider"] == "fake"
        assert item["blocked"] is False
        (entity,) = item["entities"]
        assert entity["type"] == "PERSON"
        assert entity["count"] == 2
        assert entity["mode"] == "anonymise_restore"
        assert entity["rule"]["owner"] == "admin"
        assert entity["rule"]["name"] == "Cloud providers"
        assert entity["sources"] == ["message", "tool_result"]
        assert SECRET_NAME not in orjson.dumps(events).decode()

        by_id = (await client.get("/api/me/redaction/events?request_id=req-log-1")).json()
        assert by_id["total"] == 1
        none = (await client.get("/api/me/redaction/events?request_id=other")).json()
        assert none["total"] == 0

    async def test_a_blocked_request_is_logged_as_blocked(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.BLOCK))])
        as_user(app, seeded.user)

        await client.post("/v1/chat/completions", json=body_for(seeded), headers=seeded.auth)

        item = (await client.get("/api/me/redaction/events")).json()["items"][0]
        assert item["blocked"] is True
        assert item["entities"][0]["type"] == "PERSON"
        assert item["entities"][0]["mode"] == "block"
        assert item["entities"][0]["rule"]["owner"] == "admin"

    async def test_a_request_where_nothing_was_found_leaves_no_entry(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        as_user(app, seeded.user)
        fake_upstream.set_json(completion_body())

        await client.post(
            "/v1/chat/completions", json=body_for(seeded, "nothing here"), headers=seeded.auth
        )

        assert (await client.get("/api/me/redaction/events")).json()["total"] == 0

    async def test_the_user_sees_their_own_and_the_administrator_sees_everyones_but_byok(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        install(app)
        detail = {
            "v": 1,
            "blocked": False,
            "entities": [
                {
                    "type": "PERSON",
                    "count": 1,
                    "mode": "redact",
                    "rule": None,
                    "sources": ["message"],
                }
            ],
        }
        async with session_factory() as db:
            other = User(issuer="https://idp.test", subject="other", email="other@example.org")
            db.add(other)
            await db.flush()
            for user_id, request_id, byok in (
                (seeded.user.id, "mine", False),
                (seeded.user.id, "mine-own-key", True),
                (other.id, "theirs", False),
            ):
                db.add(
                    UsageRecord(
                        request_id=request_id,
                        user_id=user_id,
                        model_name="test-model",
                        status=UsageStatus.COMPLETED,
                        byok=byok,
                        currency="EUR",
                        redaction_detail=detail,
                    )
                )
            await db.commit()
        as_user(app, seeded.user)

        mine = (await client.get("/api/me/redaction/events")).json()
        assert {i["request_id"] for i in mine["items"]} == {"mine", "mine-own-key"}
        assert {i["request_id"]: i["byok"] for i in mine["items"]}["mine-own-key"] is True

        # A non-admin is refused the admin view.
        assert (await client.get("/api/admin/redaction/events")).status_code == 403

        seeded.user.is_admin = True
        everyone = (await client.get("/api/admin/redaction/events")).json()
        assert {i["request_id"] for i in everyone["items"]} == {"mine", "theirs"}


class TestTheSummary:
    async def test_without_the_header_the_body_is_unchanged(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_json(completion_body())

        plain = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers=seeded.auth
        )
        opted = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers={**seeded.auth, **SUMMARY}
        )

        assert "pystino_redaction" not in plain.json()
        assert "pystino_cost" not in plain.json()
        opted_body = opted.json()
        assert opted_body.pop("pystino_redaction") is not None
        assert opted_body == plain.json()

    async def test_with_the_header_the_summary_is_in_the_body(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE))])
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json=body_for(seeded),
            headers={**seeded.auth, **SUMMARY, "x-request-id": "req-1"},
        )

        summary = response.json()["pystino_redaction"]
        assert summary["applied"] is True
        assert summary["request_id"] == "req-1"
        assert summary["blocked"] is False
        assert summary["entities"] == [
            {
                "type": "PERSON",
                "count": 1,
                "mode": "anonymise_restore",
                "rule": {
                    "id": summary["entities"][0]["rule"]["id"],
                    "name": "Cloud providers",
                    "owner": "admin",
                },
                "sources": ["message"],
            }
        ]
        assert "values" not in summary
        assert "pystino_cost" not in response.json()

    async def test_it_says_applied_false_when_redaction_ran_and_found_nothing(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json=body_for(seeded, "nothing to hide"),
            headers={**seeded.auth, **BOTH},
        )

        summary = response.json()["pystino_redaction"]
        assert summary["applied"] is False
        assert summary["entities"] == []

    async def test_it_is_absent_when_redaction_is_off(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        """The default engine is noop; and an engine with no rule in force is off too."""
        fake_upstream.set_json(completion_body())
        noop = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers={**seeded.auth, **SUMMARY}
        )
        assert "pystino_redaction" not in noop.json()

        install(app)  # a detecting engine, but no rule, so nothing applies
        no_rule = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers={**seeded.auth, **SUMMARY}
        )
        assert "pystino_redaction" not in no_rule.json()

    async def test_a_blocked_request_carries_it_in_the_403(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.BLOCK))])

        response = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers={**seeded.auth, **BOTH}
        )

        assert response.status_code == 403
        summary = response.json()["pystino_redaction"]
        assert summary["blocked"] is True
        assert summary["entities"][0]["type"] == "PERSON"
        assert "values" not in summary
        assert SECRET_NAME not in response.text

    async def test_streaming_unchanged_without_the_header_and_on_the_final_chunk_with_it(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_stream(
            [
                sse(chunk("Hi ")),
                sse(chunk("there")),
                sse(chunk(finish_reason="stop")),
                sse(usage_only_frame(10, 5)),
                sse("[DONE]"),
            ]
        )
        request = {**body_for(seeded), "stream": True, "stream_options": {"include_usage": True}}

        plain = await client.post("/v1/chat/completions", json=request, headers=seeded.auth)
        opted = await client.post(
            "/v1/chat/completions", json=request, headers={**seeded.auth, **SUMMARY}
        )

        def frames(raw: bytes) -> list[str]:
            return [f for f in raw.decode().split("\n\n") if f]

        plain_frames, opted_frames = frames(plain.content), frames(opted.content)
        assert len(plain_frames) == len(opted_frames)
        assert b"pystino_redaction" not in plain.content
        # Every frame but the last is byte for byte the same.
        assert plain_frames[:-2] == opted_frames[:-2]
        assert opted_frames[-1] == "data: [DONE]"
        last = orjson.loads(opted_frames[-2].removeprefix("data: "))
        assert last["usage"]["total_tokens"] == 15
        assert last["pystino_redaction"]["applied"] is True
        assert last["pystino_redaction"]["request_id"]
        assert sum(b"pystino_redaction" in f.encode() for f in opted_frames) == 1

    async def test_streaming_without_a_usage_chunk_uses_the_last_chunk_before_done(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_stream(
            [sse(chunk("Hi")), sse(chunk(finish_reason="stop")), sse("[DONE]")]
        )

        response = await client.post(
            "/v1/chat/completions",
            json={**body_for(seeded), "stream": True},
            headers={**seeded.auth, **SUMMARY},
        )

        frames = [f for f in response.content.decode().split("\n\n") if f]
        assert frames[-1] == "data: [DONE]"
        assert "pystino_redaction" in frames[-2]
        assert "pystino_redaction" not in "".join(frames[:-2])

    async def test_a_streamed_summary_reports_what_was_hidden(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.REDACT))])
        fake_upstream.set_stream(
            [
                sse(chunk("ok")),
                sse(chunk(finish_reason="stop")),
                sse(usage_only_frame(1, 1)),
                sse("[DONE]"),
            ]
        )

        response = await client.post(
            "/v1/chat/completions",
            json={**body_for(seeded), "stream": True, "stream_options": {"include_usage": True}},
            headers={**seeded.auth, **SUMMARY},
        )

        frames = [f for f in response.content.decode().split("\n\n") if f]
        last = orjson.loads(frames[-2].removeprefix("data: "))
        assert last["pystino_redaction"]["entities"][0]["count"] == 1
        assert last["pystino_redaction"]["entities"][0]["mode"] == "redact"


class TestTheValues:
    async def test_an_api_key_never_gets_the_values(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers={**seeded.auth, **BOTH}
        )

        assert "values" not in response.json()["pystino_redaction"]
        assert SECRET_NAME not in response.text

    async def test_a_user_token_without_the_second_header_does_not_either(
        self,
        app: Any,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        fake_upstream: Any,
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json=body_for(seeded),
            headers={**auth(make_token(signing_key)), **SUMMARY},
        )

        assert response.status_code == 200, response.text
        assert "values" not in response.json()["pystino_redaction"]

    async def test_both_headers_and_a_user_token_get_them(
        self,
        app: Any,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        fake_upstream: Any,
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json=body_for(seeded),
            headers={**auth(make_token(signing_key)), **BOTH},
        )

        assert response.status_code == 200, response.text
        (value,) = response.json()["pystino_redaction"]["values"]
        assert value["type"] == "PERSON"
        assert value["original"] == SECRET_NAME
        assert value["placeholder"].startswith("<PERSON_")
        # What the provider saw instead.
        assert SECRET_NAME not in orjson.dumps(fake_upstream.bodies[-1]).decode()
        assert value["placeholder"] in orjson.dumps(fake_upstream.bodies[-1]).decode()

    async def test_the_values_are_never_stored_or_logged(
        self,
        app: Any,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nothing that persists holds the original: not any column of any table,
        not the redaction log, not the application log."""
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_json(completion_body())

        with caplog.at_level(logging.DEBUG):
            response = await client.post(
                "/v1/chat/completions",
                json=body_for(seeded),
                headers={**auth(make_token(signing_key)), **BOTH},
            )
        assert response.json()["pystino_redaction"]["values"][0]["original"] == SECRET_NAME

        assert SECRET_NAME not in caplog.text
        async with session_factory() as db:
            for table in Base.metadata.sorted_tables:
                for stored in (await db.execute(select(table))).all():
                    assert SECRET_NAME not in repr(tuple(stored)), table.name
            # And the redaction log entry itself, read through the API as a user would.
            raw = (
                (await db.execute(text("select redaction_detail from usage_records")))
                .scalars()
                .all()
            )
            assert raw and all(SECRET_NAME not in str(item) for item in raw)

    async def test_streamed_values_are_not_stored_either(
        self,
        app: Any,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_stream(
            [
                sse(chunk("ok")),
                sse(chunk(finish_reason="stop")),
                sse(usage_only_frame(1, 1)),
                sse("[DONE]"),
            ]
        )

        response = await client.post(
            "/v1/chat/completions",
            json={**body_for(seeded), "stream": True, "stream_options": {"include_usage": True}},
            headers={**auth(make_token(signing_key)), **BOTH},
        )

        frames = [f for f in response.content.decode().split("\n\n") if f]
        last = orjson.loads(frames[-2].removeprefix("data: "))
        assert last["pystino_redaction"]["values"][0]["original"] == SECRET_NAME
        async with session_factory() as db:
            for table in Base.metadata.sorted_tables:
                for stored in (await db.execute(select(table))).all():
                    assert SECRET_NAME not in repr(tuple(stored)), table.name


class TestRoundTrips:
    async def test_the_log_and_the_user_rules_add_no_query_to_the_metered_path(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        """The entry rides the insert the request already makes, and the user's rules
        arrive on the resolver's poll: the budget `test_query_counts.py` pins holds
        for a request that was redacted under a user's own rule."""
        from test_query_counts import counted

        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        as_user(app, seeded.user)
        await client.post(
            "/api/me/redaction/rules",
            json={"kind": "pattern", "name": "PROJECT", "regex": r"PRJ-\d+", "mode": "redact"},
        )
        fake_upstream.set_json(completion_body())
        request = body_for(seeded, f"{SECRET_NAME} on PRJ-1")
        await client.post("/v1/chat/completions", json=request, headers=seeded.auth)

        with counted(app.state.engine) as statements:
            response = await client.post("/v1/chat/completions", json=request, headers=seeded.auth)
        assert response.status_code == 200

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert len(writes) <= 2, writes
        assert len(selects) <= 5, selects


class TestCacheInvalidation:
    def test_editing_an_administrator_rule_refreshes_a_users_folded_policy(self) -> None:
        user_id = uuid.uuid4()
        rule = catch_all(PERSON=EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE))
        resolver = resolver_with([rule], [row(user_id, mode_rule("PERSON", EntityMode.ANONYMISE))])
        assert (
            resolver.policy_for(user_id=user_id).policy.mode_for("PERSON") is EntityMode.ANONYMISE
        )

        # The same rule id, now stricter than the user's own.
        rule.policy = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)}).model_dump(
            mode="json"
        )
        resolver._reload_rules([rule])

        assert resolver.policy_for(user_id=user_id).policy.mode_for("PERSON") is EntityMode.BLOCK


class TestAUserPatternOnlyAdds:
    """A user's pattern scores 1.0, which would beat a detector's 0.85 in overlap
    resolution. It must never displace or shrink what an administrator's span covers."""

    async def _redacted(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        regex: str,
        content: str,
        mode: str = "anonymise_restore",
    ) -> str:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.REDACT))])
        as_user(app, seeded.user)
        created = await client.post(
            "/api/me/redaction/rules",
            json={"kind": "pattern", "name": "MINE", "regex": regex, "mode": mode},
        )
        assert created.status_code == 201, created.text
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=body_for(seeded, content), headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        return str(fake_upstream.bodies[-1]["messages"][-1]["content"])

    async def test_a_short_pattern_inside_a_name_does_not_shrink_the_redaction(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        sent = await self._redacted(app, client, seeded, fake_upstream, "ar", f"hi {SECRET_NAME}")

        assert sent == "hi <PERSON>"
        assert "Rossi" not in sent and "<MINE" not in sent

    async def test_a_word_character_pattern_does_not_shred_a_name(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        sent = await self._redacted(
            app, client, seeded, fake_upstream, r"\b\w", f"- {SECRET_NAME}", mode="redact"
        )

        assert sent == "- <PERSON>"

    async def test_a_weaker_user_mode_over_an_admin_span_changes_nothing(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        """anonymise_restore over the administrators' redact: nothing is restored,
        and the value never reaches the provider in part or whole."""
        sent = await self._redacted(
            app, client, seeded, fake_upstream, "Mario", f"hi {SECRET_NAME}", "anonymise_restore"
        )

        assert "Mario" not in sent and "Rossi" not in sent
        assert "<PERSON>" in sent

    async def test_a_pattern_that_overlaps_nothing_still_applies(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        sent = await self._redacted(
            app,
            client,
            seeded,
            fake_upstream,
            r"(ar|PRJ-\d+)",
            f"{SECRET_NAME} works on PRJ-42",
            mode="redact",
        )

        assert sent == "<PERSON> works on <MINE>"

    async def test_the_test_box_reports_the_same(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.REDACT))])
        as_user(app, seeded.user)
        await client.post(
            "/api/me/redaction/rules",
            json={"kind": "pattern", "name": "MINE", "regex": "ar", "mode": "block"},
        )

        body = (
            await client.post("/api/me/redaction/test", json={"text": f"hi {SECRET_NAME}"})
        ).json()

        assert [(s["entity_type"], s["rule"]["owner"]) for s in body["spans"]] == [
            ("PERSON", "admin")
        ]
        assert body["blocked"] is False

    def test_the_selection_keeps_the_administrators_span_whole(self) -> None:
        from gateway.config import CustomPattern
        from gateway.redaction.http import eligible_for
        from llmp_shared import EntitySpan

        text = f"hi {SECRET_NAME}"
        base = policy(
            default_mode=EntityMode.OFF,
            entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)},
            patterns=[CustomPattern(name="MINE", regex="ar", mode=EntityMode.BLOCK)],
        )
        detected = [EntitySpan(start=3, end=14, entity_type="PERSON", score=0.85)]

        chosen = eligible_for(text, detected, base, 0.0, frozenset({("MINE", "ar")}))
        assert [(s.entity_type, s.start, s.end) for s in chosen] == [("PERSON", 3, 14)]


class TestValuesForASignIn:
    async def test_a_house_idp_session_token_gets_the_values_and_a_created_key_does_not(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from datetime import timedelta

        from gateway.models import ApiKey
        from gateway.security import generate_api_key
        from gateway.types import utcnow

        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        fake_upstream.set_json(completion_body())
        minted = generate_api_key(environment_prefix="gwa")
        async with session_factory() as db:
            db.add(
                ApiKey(
                    user_id=seeded.user.id,
                    prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    name="idp:console",
                    minted_by="console",
                    expires_at=utcnow() + timedelta(minutes=15),
                )
            )
            await db.commit()

        session_token = await client.post(
            "/v1/chat/completions",
            json=body_for(seeded),
            headers={"authorization": f"Bearer {minted.secret}", **BOTH},
        )
        created_key = await client.post(
            "/v1/chat/completions", json=body_for(seeded), headers={**seeded.auth, **BOTH}
        )

        assert session_token.json()["pystino_redaction"]["values"][0]["original"] == SECRET_NAME
        assert "values" not in created_key.json()["pystino_redaction"]


class TestTheTestBoxIsRateLimited:
    async def test_the_thirty_first_call_in_a_minute_is_refused(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        for _ in range(30):
            assert (
                await client.post("/api/me/redaction/test", json={"text": "x"})
            ).status_code == 200

        over = await client.post("/api/me/redaction/test", json={"text": "x"})

        assert over.status_code == 429
        assert "at most 30 texts a minute" in over.json()["error"]["message"]


class TestLowerFindings:
    async def test_rules_in_force_leave_out_another_users_private_provider(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from gateway.models import Provider

        install(app)
        async with session_factory() as db:
            other = User(issuer="https://idp.test", subject="other", email="o@example.org")
            db.add(other)
            await db.flush()
            private = Provider(
                name="theirs",
                base_url="https://x.example/v1",
                api_key_encrypted="",
                api_key_hint="",
                owner_kind="user",
                owner_id=other.id,
            )
            db.add(private)
            await db.flush()
            db.add(admin_rule(RedactionScope.PROVIDER, private.id, "Their provider"))
            db.add(admin_rule(RedactionScope.PROVIDER, seeded.provider.id, "Deployment provider"))
            await db.commit()
        as_user(app, seeded.user)

        names = [
            r["name"] for r in (await client.get("/api/me/redaction/in-force")).json()["rules"]
        ]

        assert names == ["Deployment provider"]

    async def test_the_admin_preview_as_a_key_includes_its_owners_rules(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        install(app)
        as_user(app, seeded.user)
        await client.post(
            "/api/me/redaction/rules",
            json={"kind": "mode", "name": "EMAIL_ADDRESS", "mode": "redact"},
        )
        seeded.user.is_admin = True

        response = await client.post(
            "/api/admin/redaction/preview",
            json={
                "text": "write to a@example.org",
                "scope": "api_key",
                "scope_id": str(seeded.api_key.id),
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["policy"]["entities"]["EMAIL_ADDRESS"]["mode"] == "redact"

    async def test_a_held_finish_chunk_is_flushed_before_an_upstream_error(self) -> None:
        from collections.abc import AsyncIterator

        from gateway.routers.chat import redaction_summary_stage
        from gateway.sse.events import SSEEvent

        async def upstream() -> AsyncIterator[SSEEvent]:
            yield SSEEvent.from_json(chunk("Hi"))
            yield SSEEvent.from_json(chunk(finish_reason="stop"))
            raise RuntimeError("the upstream dropped")

        seen: list[SSEEvent] = []
        with pytest.raises(RuntimeError):
            async for event in redaction_summary_stage({"applied": False})(upstream()):
                seen.append(event)

        assert len(seen) == 2
        assert "pystino_redaction" not in seen[0].data
        assert "pystino_redaction" in seen[1].data

    async def test_the_creation_cap_takes_a_row_lock_and_still_holds(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """The count and the insert run under a lock on the user's row; the cap
        itself is exercised in TestTheManagementApi."""
        from gateway.redaction.user_rules import MAX_RULES_PER_USER

        install(app)
        as_user(app, seeded.user)
        for index in range(MAX_RULES_PER_USER):
            await client.post(
                "/api/me/redaction/rules",
                json={"kind": "mode", "name": f"T{index}", "mode": "redact"},
            )

        over = await client.post(
            "/api/me/redaction/rules", json={"kind": "mode", "name": "EXTRA", "mode": "redact"}
        )

        assert over.status_code == 400
