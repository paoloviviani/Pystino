"""Policies folded across the pipelines in scope (ADR 0038, ADR 0113).

The property this file exists for is one sentence: **adding a pipeline can only
tighten**. It is enforced by the choice of combiners rather than by validation,
so the tests are about the fold: a policy that tries to weaken must be inert, not
rejected, because inert is what survives a bug in whatever wrote it. (Scoping
moved from the rules to the pipelines that apply them; the fold did not move.)

The second property is provenance: the policies are mutable, so a request that
does not record which policy tightened it cannot be explained afterwards, and
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
)
from gateway.models import UsageRecord, UsageStatus
from gateway.privacy.state import PolicyRef, PrivacyState
from helpers import completion_body
from privacy_helpers import add_pipeline, add_policy
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

    def test_a_stricter_pattern_of_the_same_name_cannot_hide_a_lower_threshold(self) -> None:
        """Policy A finds PERSON and PHONE_NUMBER at 0.3 and redacts them; policy B has
        patterns of the same names in block. The strictest-mode entry is the pattern, which
        has no threshold, so the type used to fall back to the deployment's 0.5 and hide
        less than A asked."""
        a = policy(
            entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT, threshold=0.3)},
            formats={"PHONE_NUMBER": EntityPolicy(mode=EntityMode.REDACT, threshold=0.3)},
        )
        b = policy(
            patterns=[
                CustomPattern(name="PERSON", regex="aaa", mode=EntityMode.BLOCK),
                CustomPattern(name="PHONE_NUMBER", regex="bbb", mode=EntityMode.BLOCK),
            ]
        )
        combined = RedactionPolicy.combine([a, b])
        assert combined.mode_for("PERSON") is EntityMode.BLOCK
        assert combined.threshold_for("PERSON", 0.5) == 0.3
        assert combined.threshold_for("PHONE_NUMBER", 0.5) == 0.3
        # An entry that is off does not set one, and with none the default stands.
        off = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.OFF, threshold=0.1)})
        assert off.threshold_for("PERSON", 0.5) == 0.5

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


class TestResolution:
    """From the policies the pipelines chose to the one policy that governs a request."""

    user = uuid.uuid4()

    def state(self, **named: RedactionPolicy) -> tuple[PrivacyState, dict[str, str]]:
        state = PrivacyState()
        ids: dict[str, str] = {}
        for name, p in named.items():
            ref = PolicyRef(uuid.uuid4(), name, p)
            state.policies[str(ref.id)] = ref
            ids[name] = str(ref.id)
        return state, ids

    async def effective(
        self, state: PrivacyState, ids: dict[str, str], scopes: list[tuple[str, dict[str, Any]]]
    ) -> Any:
        from gateway.models import PrivacyPipeline
        from gateway.privacy.combine import plan
        from gateway.privacy.evaluate import Facts
        from gateway.privacy.pipelines import pipeline_from_row

        pipelines = []
        for index, (name, who) in enumerate(scopes):
            pipelines.append(
                pipeline_from_row(
                    PrivacyPipeline(
                        id=uuid.uuid4(),
                        owner_id=None,
                        kind="chat",
                        name=name,
                        enabled=True,
                        position=index,
                        who=who,
                        target={"mode": "all", "providers": [], "models": []},
                        rules=[],
                        otherwise={"level": "pass", "redact_with": ids[name]},
                    ),
                    order=index,
                )
            )
        facts = Facts(kind="chat", user_id=str(self.user))
        combined = await plan(state.evaluator, pipelines, facts)
        return state.effective_policy(combined.policy_uses(), RedactionPolicy())

    async def test_no_policy_means_the_deployment_policy_alone(self) -> None:
        state, ids = self.state()
        effective = await self.effective(state, ids, [])
        assert effective.scope is None and effective.rule_id is None
        assert effective.policy == RedactionPolicy()
        assert effective.contributors[0].name == "Deployment policy"

    async def test_the_narrowest_policy_is_the_one_recorded(self) -> None:
        wide = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE)})
        narrow = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)})
        state, ids = self.state(Wide=wide, Narrow=narrow)
        everyone = {"mode": "all", "ids": []}
        mine = {"mode": "users", "ids": [str(self.user)]}
        effective = await self.effective(state, ids, [("Narrow", mine), ("Wide", everyone)])
        assert effective.policy.mode_for("PERSON") is EntityMode.BLOCK
        # Widest first, as the fold has always run; the narrowest is the one recorded.
        assert [c.name for c in effective.contributors] == ["Wide", "Narrow"]
        assert effective.scope == "user"
        assert str(effective.rule_id) == ids["Narrow"]

    async def test_a_pipeline_for_someone_else_applies_no_policy(self) -> None:
        stricter = policy(entities={"PERSON": EntityPolicy(mode=EntityMode.BLOCK)})
        state, ids = self.state(Other=stricter)
        effective = await self.effective(
            state, ids, [("Other", {"mode": "users", "ids": [str(uuid.uuid4())]})]
        )
        assert effective.policy.mode_for("PERSON") is EntityMode.OFF

    def test_an_unreadable_policy_is_dropped_not_fatal(self) -> None:
        from gateway.models import RedactionPolicyRecord

        good = RedactionPolicyRecord(
            id=uuid.uuid4(), name="Good", policy=policy(), is_active=True, reason=""
        )
        bad = RedactionPolicyRecord(
            id=uuid.uuid4(),
            name="Bad",
            policy={"default_mode": "sideways"},
            is_active=True,
            reason="",
        )
        state = PrivacyState()
        state.load([], [good, bad], [], [], [], [])
        assert list(state.policies) == [str(good.id)]
        assert state.bad_rows and str(bad.id) in state.bad_rows[0]

    async def test_the_fold_is_memoised_and_cleared_on_load(self) -> None:
        p = policy(entities={"PERSON": EntityPolicy()})
        state, ids = self.state(A=p, B=p)
        everyone = {"mode": "all", "ids": []}
        await self.effective(state, ids, [("A", everyone), ("B", everyone)])
        assert len(state._folded) == 1
        state.load([], [], [], [], [], [])
        assert state._folded == {}


class TestBlocking:
    """A refused request: 403, no provider call, and a row that says so."""

    async def install(
        self, app: Any, session_factory: Any, *, mode: EntityMode, who: dict[str, Any]
    ) -> uuid.UUID:
        """A detector that always finds an SSN, and a policy about it."""
        detector = FakeDetector({"123-45-6789": "US_SSN"})
        app.state.redactor = detector.redactor()
        pid = await add_policy(
            app,
            session_factory,
            "SSN",
            entities={"US_SSN": EntityPolicy(mode=mode)},
        )
        await add_pipeline(
            app,
            session_factory,
            "chat",
            "SSN pipeline",
            who=who,
            otherwise={"level": "pass", "redact_with": str(pid)},
        )
        return pid

    async def test_a_blocked_request_is_refused_and_never_reaches_the_provider(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
        fake_upstream: Any,
    ) -> None:
        pid = await self.install(
            app,
            session_factory,
            mode=EntityMode.BLOCK,
            who={"mode": "users", "ids": [str(seeded.user.id)]},
        )
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
        assert row.redaction_scope == "user"
        assert row.redaction_rule_id == pid
        assert row.error_code == "content_blocked"

    async def test_the_same_entity_under_a_weaker_mode_is_served(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        fake_upstream: Any,
    ) -> None:
        """The block is the policy's doing, not the detector's: same detection,
        different mode, and the request goes through redacted."""
        await self.install(
            app,
            session_factory,
            mode=EntityMode.REDACT,
            who={"mode": "users", "ids": [str(seeded.user.id)]},
        )
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
        # The user's turn is last: a policy in force also adds a system note first.
        sent = fake_upstream.bodies[-1]["messages"][-1]["content"]
        assert "123-45-6789" not in sent
        assert "<US_SSN>" in sent

    async def test_a_pipeline_for_another_user_does_not_block_this_one(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        fake_upstream: Any,
    ) -> None:
        await self.install(
            app,
            session_factory,
            mode=EntityMode.BLOCK,
            who={"mode": "users", "ids": [str(uuid.uuid4())]},
        )
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
