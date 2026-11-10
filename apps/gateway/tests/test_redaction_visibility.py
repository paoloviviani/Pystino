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
from dataclasses import dataclass, field
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
)
from gateway.deps import get_management_user
from gateway.models import (
    Base,
    PrivacyPipeline,
    RedactionPolicyRecord,
    UsageRecord,
    UsageStatus,
    User,
    UserPolicy,
)
from gateway.privacy.combine import plan
from gateway.privacy.evaluate import Facts
from gateway.privacy.state import PrivacyState
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


@dataclass
class AdminSpec:
    """An administrator's policy, applied to every request of every kind."""

    name: str
    policy: RedactionPolicy
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def admin_rule(name: str, **kw: Any) -> AdminSpec:
    return AdminSpec(name=name, policy=policy(**kw))


def state_with(
    admin: list[AdminSpec] | None = None,
    users: dict[uuid.UUID, list[UserRule]] | None = None,
) -> PrivacyState:
    """A privacy state holding these policies, each applied by a pipeline on all (v2 shapes)."""
    import privacy_helpers as ph

    policies = []
    for a in admin or []:
        stored, modes = ph._M.convert_policy(a.policy.model_dump(mode="json"), [], a.name)
        ph.PMODES[str(a.id)] = modes
        policies.append(
            RedactionPolicyRecord(id=a.id, name=a.name, policy=stored, is_active=True, reason="")
        )
    pipelines: list[PrivacyPipeline] = []

    def rules_for(pid: uuid.UUID, name: str) -> list[dict[str, Any]]:
        return ph._M.convert_content_rules(
            [], {"level": "pass", "redact_with": str(pid)}, ph.PMODES, [], name
        )

    for index, a in enumerate(admin or []):
        pipelines.append(
            PrivacyPipeline(
                id=uuid.uuid4(),
                owner_id=None,
                kind="content",
                name=a.name,
                enabled=True,
                position=index,
                who={"mode": "all", "ids": []},
                target={"mode": "all", "providers": [], "models": [], "model_types": []},
                rules=rules_for(a.id, a.name),
            )
        )
    user_policies: list[UserPolicy] = []
    for user_id, rules in (users or {}).items():
        mine = UserPolicy(
            id=uuid.uuid4(),
            user_id=user_id,
            name="Mine",
            rules=[
                {"kind": r.kind, "name": r.name, "regex": r.regex, "mode": r.mode.value}
                for r in rules
            ],
        )
        stored_entries, modes = ph._M.convert_user_entries(list(mine.rules))
        mine.rules = stored_entries
        ph.PMODES[str(mine.id)] = modes
        user_policies.append(mine)
        pipelines.append(
            PrivacyPipeline(
                id=uuid.uuid4(),
                owner_id=user_id,
                kind="content",
                name="Mine",
                enabled=True,
                position=0,
                who={"mode": "users", "ids": [str(user_id)]},
                target={"mode": "all", "providers": [], "models": [], "model_types": []},
                rules=rules_for(mine.id, "Mine"),
            )
        )
    state = PrivacyState()
    state.load(pipelines, policies, user_policies, [], [], [])
    return state


async def effective(state: PrivacyState, user_id: uuid.UUID) -> Any:
    combined = await plan(
        state.evaluator, state.pipelines_of("content"), Facts(kind="content", user_id=str(user_id))
    )
    return state.effective_policy(combined.hiding_hits(), RedactionPolicy())


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

    async def test_a_user_pattern_applies_only_to_that_user(self) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        state = state_with(users={mine: [pattern_rule("PROJECT", r"PRJ-\d+")]})

        assert [p.name for p in (await effective(state, mine)).policy.patterns] == ["PROJECT"]
        assert (await effective(state, theirs)).policy.patterns == []

    async def test_the_administrators_answer_is_unchanged_for_a_user_with_no_rules(self) -> None:
        everything = admin_rule("Everything", entities={"PERSON": EntityPolicy()})
        state = state_with([everything], {uuid.uuid4(): [mode_rule("PERSON", EntityMode.BLOCK)]})

        found = await effective(state, uuid.uuid4())
        assert found.policy.mode_for("PERSON") is EntityMode.ANONYMISE_RESTORE

    def test_an_unreadable_row_is_dropped_not_fatal(self) -> None:
        good = uuid.uuid4()
        bad = uuid.uuid4()
        state = PrivacyState()
        state.load(
            [],
            [],
            [
                UserPolicy(
                    id=uuid.uuid4(),
                    user_id=bad,
                    name="Bad",
                    rules=[{"kind": "pattern", "name": "X", "regex": "(?=no)", "mode": "redact"}],
                ),
                UserPolicy(
                    id=uuid.uuid4(),
                    user_id=good,
                    name="Good",
                    rules=[{"kind": "mode", "name": "PERSON", "regex": None, "mode": "redact"}],
                ),
            ],
            [],
            [],
            [],
        )

        assert [p.user_id for p in state.user_policies.values()] == [good]


# ---------------------------------------------------------------------------
# through the application
# ---------------------------------------------------------------------------


def as_user(app: Any, user: User) -> None:
    app.dependency_overrides[get_management_user] = lambda: user


def install(
    app: Any,
    *,
    admin: list[AdminSpec] | None = None,
    users: dict[uuid.UUID, list[UserRule]] | None = None,
    detects: dict[str, str] | None = None,
) -> PrivacyState:
    """A detector that finds what it is told to, and a state holding these policies."""
    detector = FakeDetector(detects if detects is not None else {SECRET_NAME: "PERSON"})
    app.state.redactor = detector.redactor()
    app.state._test_admin = list(admin or [])
    app.state._test_users = dict(users or {})
    state = state_with(admin, users)
    # Detached from the database: no poll may put the empty rows back mid-test.
    app.state.privacy = state
    return state


def give_user_rules(app: Any, user_id: uuid.UUID, *rules: UserRule) -> None:
    """The user's own rules, as a user policy their chat pipeline applies."""
    mine = app.state._test_users.setdefault(user_id, [])
    mine.extend(rules)
    app.state.privacy = state_with(app.state._test_admin, app.state._test_users)


def catch_all(**entities: EntityPolicy) -> AdminSpec:
    return admin_rule("Cloud providers", entities=entities)


def body_for(seeded: Seeded, content: str = f"hello {SECRET_NAME}") -> dict[str, Any]:
    return {"model": seeded.model.name, "messages": [{"role": "user", "content": content}]}


SUMMARY = {"x-pystino-redaction-summary": "1"}
BOTH = {**SUMMARY, "x-pystino-redaction-values": "1"}


class TestOnTheRequestPath:
    async def test_a_user_pattern_redacts_that_users_requests_and_only_theirs(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # An administrator's policy makes redaction active; the user's pattern adds to it.
        state = install(app, detects={}, admin=[catch_all(PERSON=EntityPolicy())])
        as_user(app, seeded.user)
        give_user_rules(app, seeded.user.id, pattern_rule("PROJECT", r"PRJ-\d+"))
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
        assert (await effective(app.state.privacy, other.id)).policy.patterns == []
        del state

    async def test_a_user_tightening_applies_and_a_weaker_rule_does_not(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
    ) -> None:
        install(app, admin=[catch_all(PERSON=EntityPolicy(mode=EntityMode.REDACT))])
        as_user(app, seeded.user)
        give_user_rules(app, seeded.user.id, mode_rule("PERSON", EntityMode.ANONYMISE_RESTORE))
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
        give_user_rules(app, seeded.user.id, mode_rule("PERSON", EntityMode.REDACT))
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
        assert entity["rule"]["name"] == "Everything else"
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
        # A block is a rule's (ADR 0114): the entry names the rule that refused, not a type.
        assert item["routing"]["outcome"] == "refused"
        assert item["routing"]["handled"]["routing_rule"]["owner"] == "admin"

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
                    "name": "Everything else",
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
        assert summary["routing"]["outcome"] == "refused"
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
        arrive on the state's poll: the budget `test_query_counts.py` pins holds
        for a request that was redacted under a user's own rule."""
        from test_query_counts import counted

        install(app, admin=[catch_all(PERSON=EntityPolicy())])
        as_user(app, seeded.user)
        give_user_rules(app, seeded.user.id, pattern_rule("PROJECT", r"PRJ-\d+"))
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
        give_user_rules(app, seeded.user.id, pattern_rule("MINE", regex, EntityMode(mode)))
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


class TestLowerFindings:
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
