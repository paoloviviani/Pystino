"""Redaction scoped per provider, model, group, user and key (ADR 0038).

The property this file exists for is one sentence: **adding a scope can only
tighten**. It is enforced by the choice of combiners rather than by validation,
so the tests are about the fold — a rule that tries to weaken must be inert, not
rejected, because inert is what survives a bug in whatever wrote it.

The second property is provenance: the rules table is mutable, so a request that
does not record which rule tightened it cannot be explained afterwards, and
"why was this redacted" is the question a data-protection review asks.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from conftest import Seeded
from gateway.config import (
    DEFAULT_REDACTION_POLICY,
    CustomPattern,
    EntityMode,
    EntityPolicy,
    RedactionPolicy,
    RedactionSettings,
)
from gateway.models import RedactionRule, RedactionScope, UsageRecord, UsageStatus
from gateway.redaction.resolver import RedactionResolver, _rules_of
from helpers import completion_body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_redaction_http import FakeDetector


def policy(**kwargs: object) -> RedactionPolicy:
    return RedactionPolicy(**kwargs)  # type: ignore[arg-type]


class TestTheFold:
    def test_a_scope_cannot_weaken_what_the_deployment_set(self) -> None:
        deployment = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})
        wishful = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.OFF)})

        combined = RedactionPolicy.combine([deployment, wishful])
        assert combined.mode_for("PERSON") is EntityMode.REDACT

    def test_a_scope_can_strengthen(self) -> None:
        deployment = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)})
        stricter = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)})

        combined = RedactionPolicy.combine([deployment, stricter])
        assert combined.mode_for("PERSON") is EntityMode.BLOCK

    def test_a_narrow_rule_does_not_disturb_what_a_wider_one_decided(self) -> None:
        """A policy contributes where it *names* a type and is silent elsewhere.

        Both directions matter and both were bugs at some point: a rule about
        IBAN must not switch EMAIL_ADDRESS on (its own default would, if
        defaults fell through), and it must not switch URL off either.
        """
        catch_all = policy(
            entities={
                "EMAIL_ADDRESS": EntityPolicy(),
                "URL": EntityPolicy(mode=EntityMode.OFF),
            }
        )
        combined = RedactionPolicy.combine(
            [catch_all, policy(entities={"IBAN_CODE": EntityPolicy()})]
        )

        assert combined.mode_for("EMAIL_ADDRESS") is EntityMode.ANONYMISE_RESTORE
        assert combined.mode_for("URL") is EntityMode.OFF
        assert combined.mode_for("IBAN_CODE") is EntityMode.ANONYMISE_RESTORE

    def test_the_strictest_default_wins(self) -> None:
        combined = RedactionPolicy.combine(
            [policy(default_mode=EntityMode.OFF), policy(default_mode=EntityMode.REDACT)]
        )
        assert combined.default_mode is EntityMode.REDACT

    def test_thresholds_fold_to_the_most_sensitive(self) -> None:
        """A raised threshold catches fewer spans, so `min` is the strict
        direction — and a group rule cannot hide names by demanding 0.95."""
        strict = policy(entities={"PERSON": EntityPolicy(threshold=0.5)})
        lax = policy(entities={"PERSON": EntityPolicy(threshold=0.95)})

        assert RedactionPolicy.combine([strict, lax]).threshold_for("PERSON", 0.0) == 0.5

    def test_patterns_are_unioned(self) -> None:
        a = policy(patterns=[CustomPattern(name="PRJ", regex=r"PRJ-\d+")])
        b = policy(patterns=[CustomPattern(name="TICKET", regex=r"T-\d+")])

        combined = RedactionPolicy.combine([a, b])
        assert {p.name for p in combined.patterns} == {"PRJ", "TICKET"}

    def test_the_allow_list_comes_from_the_deployment_alone(self) -> None:
        """The one field that weakens, so it is not a per-scope field at all.

        Union would let a user exempt what an admin redacts. Intersection is
        worse: the deployment's own exemptions would vanish the moment any scoped
        rule carried an empty list, and the failure looks like over-redaction
        with nothing on screen to explain it.
        """
        deployment = policy(allow_list=["acme.test"])
        scoped = policy(allow_list=["evil.test"])

        assert RedactionPolicy.combine([deployment, scoped]).allow_list == ["acme.test"]

    def test_one_policy_folds_to_itself(self) -> None:
        assert RedactionPolicy.combine([DEFAULT_REDACTION_POLICY]) is DEFAULT_REDACTION_POLICY


class TestTheCatchAll:
    """The scope that is every request, and is a rule like any other (ADR 0039)."""

    def resolver_with(self, *rules: RedactionRule) -> RedactionResolver:
        resolver = RedactionResolver(RedactionSettings(), lambda: None)  # type: ignore[arg-type]
        resolver._reload_rules(list(rules))
        return resolver

    def rule(
        self, scope: RedactionScope, subject: uuid.UUID | None, **entities: EntityPolicy
    ) -> RedactionRule:
        return RedactionRule(
            id=uuid.uuid4(),
            scope=scope,
            scope_id=subject,
            policy=policy(entities=entities).model_dump(mode="json"),
        )

    def test_with_no_rules_at_all_nothing_is_redacted(self) -> None:
        """The deployment default since ADR 0039, and the reason the catch-all
        exists: turning redaction on is writing one rule, not editing a separate
        object with its own screen and its own shape."""
        effective = self.resolver_with().policy_for(user_id=uuid.uuid4())

        assert effective.policy.entities == {}
        assert effective.policy.default_mode is EntityMode.OFF
        assert effective.scope is None

    def test_it_applies_to_a_request_with_no_other_rule(self) -> None:
        catch_all = self.rule(RedactionScope.ALL, None, PERSON=EntityPolicy())
        effective = self.resolver_with(catch_all).policy_for(user_id=uuid.uuid4())

        assert effective.policy.mode_for("PERSON") is EntityMode.ANONYMISE_RESTORE
        assert effective.scope == RedactionScope.ALL
        assert effective.rule_id == catch_all.id

    def test_a_narrower_rule_tightens_it_and_takes_the_blame(self) -> None:
        user_id = uuid.uuid4()
        catch_all = self.rule(RedactionScope.ALL, None, PERSON=EntityPolicy())
        theirs = self.rule(RedactionScope.USER, user_id, PERSON=EntityPolicy(mode=EntityMode.BLOCK))
        effective = self.resolver_with(catch_all, theirs).policy_for(user_id=user_id)

        assert effective.policy.mode_for("PERSON") is EntityMode.BLOCK
        # The narrowest is recorded, which is the one somebody set for this
        # subject — the catch-all still applied.
        assert effective.rule_id == theirs.id

    def test_a_narrower_rule_cannot_undo_it(self) -> None:
        user_id = uuid.uuid4()
        catch_all = self.rule(RedactionScope.ALL, None, PERSON=EntityPolicy(mode=EntityMode.REDACT))
        wishful = self.rule(RedactionScope.USER, user_id, PERSON=EntityPolicy(mode=EntityMode.OFF))
        effective = self.resolver_with(catch_all, wishful).policy_for(user_id=user_id)

        assert effective.policy.mode_for("PERSON") is EntityMode.REDACT

    def test_its_allow_list_is_the_one_that_counts(self) -> None:
        """`combine` takes the allow-list from the first policy, and the first is
        the catch-all: an exemption belongs to the broadest statement anyone has
        made, or a narrow rule could exempt what a wide one protects."""
        user_id = uuid.uuid4()
        catch_all = RedactionRule(
            id=uuid.uuid4(),
            scope=RedactionScope.ALL,
            scope_id=None,
            policy=policy(allow_list=["acme.test"]).model_dump(mode="json"),
        )
        theirs = RedactionRule(
            id=uuid.uuid4(),
            scope=RedactionScope.USER,
            scope_id=user_id,
            policy=policy(allow_list=["evil.test"]).model_dump(mode="json"),
        )
        effective = self.resolver_with(catch_all, theirs).policy_for(user_id=user_id)

        assert effective.policy.allow_list == ["acme.test"]


class TestResolution:
    """Which rules apply to a request, and which one gets the blame."""

    def resolver(self) -> RedactionResolver:
        """A resolver with no database behind it: these tests are about the fold
        and the lookup, both of which happen entirely in memory."""
        return RedactionResolver(RedactionSettings(), lambda: None)  # type: ignore[arg-type]

    def test_no_rules_means_the_deployment_policy_alone(self) -> None:
        resolver = self.resolver()
        effective = resolver.policy_for(user_id=uuid.uuid4())

        assert effective.policy is resolver.policy
        assert effective.scope is None and effective.rule_id is None

    def test_the_narrowest_rule_is_the_one_recorded(self) -> None:
        """A request under several rules names one, and it is the one somebody
        set deliberately for the narrowest subject — the first place an operator
        looks. The others are on the rules screen; ADR 0038 says so rather than
        implying the trail is complete."""
        group_id, user_id = uuid.uuid4(), uuid.uuid4()
        rows = [
            RedactionRule(
                id=uuid.uuid4(),
                scope=RedactionScope.GROUP,
                scope_id=group_id,
                policy=policy(entities={"URL": EntityPolicy(mode=EntityMode.REDACT)}).model_dump(
                    mode="json"
                ),
            ),
            RedactionRule(
                id=uuid.uuid4(),
                scope=RedactionScope.USER,
                scope_id=user_id,
                policy=policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)}).model_dump(
                    mode="json"
                ),
            ),
        ]
        resolver = self.resolver()
        resolver._reload_rules(rows)

        effective = resolver.policy_for(group_id=group_id, user_id=user_id)

        assert effective.scope == RedactionScope.USER
        assert effective.rule_id == rows[1].id
        # Both still applied, which is the point of recording only the narrowest
        # being a documented limitation rather than a lost rule.
        assert effective.policy.mode_for("URL") is EntityMode.REDACT
        assert effective.policy.mode_for("PERSON") is EntityMode.BLOCK

    def test_a_rule_for_someone_else_does_not_apply(self) -> None:
        resolver = self.resolver()
        resolver._reload_rules(
            [
                RedactionRule(
                    id=uuid.uuid4(),
                    scope=RedactionScope.USER,
                    scope_id=uuid.uuid4(),
                    policy=policy(default_mode=EntityMode.BLOCK).model_dump(mode="json"),
                )
            ]
        )

        assert resolver.policy_for(user_id=uuid.uuid4()).scope is None

    def test_an_unreadable_rule_is_dropped_not_fatal(self) -> None:
        """One malformed row must not stop a worker resolving policy for every
        other scope — that failure switches redaction off for everyone because
        one group's rule has a typo."""
        good_id = uuid.uuid4()
        parsed = _rules_of(
            [
                RedactionRule(
                    id=uuid.uuid4(),
                    scope=RedactionScope.GROUP,
                    scope_id=uuid.uuid4(),
                    policy={"default_mode": "obliterate"},
                ),
                RedactionRule(
                    id=uuid.uuid4(),
                    scope=RedactionScope.USER,
                    scope_id=good_id,
                    policy=policy().model_dump(mode="json"),
                ),
            ]
        )

        assert list(parsed) == [(RedactionScope.USER.value, good_id)]

    def test_reloading_rules_does_not_rebuild_the_redactor(self) -> None:
        """The redactor owns the detection LRU, which is the difference between
        13ms and 135ms on a 1,000-token prompt. A rule edit must not cost it."""
        resolver = self.resolver()
        before = resolver.redactor

        changed = resolver._reload_rules(
            [
                RedactionRule(
                    id=uuid.uuid4(),
                    scope=RedactionScope.GROUP,
                    scope_id=uuid.uuid4(),
                    policy=policy().model_dump(mode="json"),
                )
            ]
        )

        assert changed is True
        assert resolver.redactor is before

    def test_the_fold_is_memoised_and_cleared_on_reload(self) -> None:
        user_id = uuid.uuid4()
        rows = [
            RedactionRule(
                id=uuid.uuid4(),
                scope=RedactionScope.USER,
                scope_id=user_id,
                policy=policy(entities={"URL": EntityPolicy(mode=EntityMode.REDACT)}).model_dump(
                    mode="json"
                ),
            )
        ]
        resolver = self.resolver()
        resolver._reload_rules(rows)

        first = resolver.policy_for(user_id=user_id).policy
        assert resolver.policy_for(user_id=user_id).policy is first

        resolver._reload_rules([])
        assert resolver.policy_for(user_id=user_id).scope is None


class TestBlocking:
    """A refused request: 403, no provider call, and a row that says so."""

    def install(self, app: object, *, mode: EntityMode, scope_id: uuid.UUID) -> RedactionRule:
        """A detector that always finds an SSN, and a user rule about it."""
        detector = FakeDetector({"123-45-6789": "US_SSN"})
        app.state.redactor = detector.redactor()  # type: ignore[attr-defined]
        rule = RedactionRule(
            id=uuid.uuid4(),
            scope=RedactionScope.USER,
            scope_id=scope_id,
            policy=policy(entities={"US_SSN": EntityPolicy(mode=mode)}).model_dump(mode="json"),
        )
        resolver = RedactionResolver(RedactionSettings(), lambda: None)  # type: ignore[arg-type]
        resolver._reload_rules([rule])
        app.state.redaction = resolver  # type: ignore[attr-defined]
        return rule

    async def test_a_blocked_request_is_refused_and_never_reaches_the_provider(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: Any,
    ) -> None:
        rule = self.install(app, mode=EntityMode.BLOCK, scope_id=seeded.user.id)
        before = len(fake_upstream.bodies)

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": "my ssn is 123-45-6789"}],
            },
            headers=seeded.auth,
        )

        assert response.status_code == 403, response.text
        body = response.json()["error"]
        assert body["code"] == "content_blocked"
        assert "US_SSN" in body["message"]
        # The value itself is never echoed: an error body is logged, and pasted
        # into tickets. Leaking there is what the block exists to prevent.
        assert "123-45-6789" not in response.text
        assert len(fake_upstream.bodies) == before

        row = (await session.execute(select(UsageRecord))).scalars().one()
        assert row.status is UsageStatus.BLOCKED
        assert row.cost == 0 and row.total_tokens == 0
        assert row.redaction_scope == RedactionScope.USER
        assert row.redaction_rule_id == rule.id
        assert row.error_code == "content_blocked"

    async def test_the_same_entity_under_a_weaker_mode_is_served(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
    ) -> None:
        """The block is the rule's doing, not the detector's — same detection,
        different mode, and the request goes through redacted."""
        self.install(app, mode=EntityMode.REDACT, scope_id=seeded.user.id)
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": "my ssn is 123-45-6789"}],
            },
            headers=seeded.auth,
        )

        assert response.status_code == 200, response.text
        sent = fake_upstream.bodies[-1]["messages"][0]["content"]
        assert "123-45-6789" not in sent
        assert "<US_SSN>" in sent

    async def test_a_rule_for_another_user_does_not_block_this_one(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
    ) -> None:
        self.install(app, mode=EntityMode.BLOCK, scope_id=uuid.uuid4())
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": "my ssn is 123-45-6789"}],
            },
            headers=seeded.auth,
        )

        assert response.status_code == 200, response.text
