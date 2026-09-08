"""ADR 0058: provider-side web search is charged per search, and capped.

The unit itself is tested in `test_cost.py`. What is tested here is the part
that touches a live request: the bound that goes onto the reservation, the cap
written into the outgoing tool definition, and the count landing on the ledger
row.

Two properties carry the weight.

**The number reserved and the number enforced are the same number.** A cap we
hold budget against but do not send is a figure that looks like a ceiling and
is not one — the OCR surface is stuck with that shape because a document has no
page-count field to write into, and this surface is not.

**The count comes only from what the provider reports.** Never from counting
`server_tool_use` blocks in the response: a search that errors produces a block
and, Anthropic states, is not billed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import LimitMetric, LimitScope, ModelPrice, UsageRecord
from gateway.routers._metered import bound_web_search
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_quota_engine import add_rule

#: Anthropic's tool, date-versioned. The version is what tells us `max_uses`
#: exists on it.
ANTHROPIC = {"type": "web_search_20250305", "name": "web_search"}
#: OpenAI's, whose options are `search_context_size` and `filters` — no cap.
OPENAI = {"type": "web_search"}


async def latest_record(session: AsyncSession) -> UsageRecord:
    return (
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        )
        .scalars()
        .one()
    )


async def price_searches(session: AsyncSession, seeded: Seeded, *, per_search: str) -> None:
    """Give the seeded model a per-search rate, effective now."""
    session.add(
        ModelPrice(
            model_id=seeded.model.id,
            input_per_mtok=Decimal(1),
            output_per_mtok=Decimal(2),
            per_search=Decimal(per_search),
            currency="EUR",
        )
    )
    await session.commit()


def messages_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "who won yesterday"}],
    }
    body.update(extra)
    return body


def searched(requests: int) -> dict[str, Any]:
    return {
        "id": "m1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "they did"}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "server_tool_use": {"web_search_requests": requests},
        },
    }


class TestTheBound:
    def test_a_request_without_search_reserves_nothing(self) -> None:
        """This must not put a cost on every ordinary completion."""
        assert bound_web_search(None, default=5).reserved == 0
        assert bound_web_search([], default=5).reserved == 0
        assert bound_web_search([{"type": "function"}], default=5).reserved == 0

    def test_the_callers_own_cap_is_taken_as_given(self) -> None:
        bound = bound_web_search([{**ANTHROPIC, "max_uses": 3}], default=5)
        assert bound.reserved == 3
        assert bound.enforced is True

    def test_an_uncapped_request_reserves_the_deployment_default(self) -> None:
        bound = bound_web_search([ANTHROPIC], default=5)
        assert bound.reserved == 5
        assert bound.enforced is True

    def test_the_default_is_written_into_the_tool(self) -> None:
        """What makes the reservation a real bound rather than a hope."""
        payload = {"tools": [dict(ANTHROPIC)]}
        bound_web_search(payload["tools"], default=5).apply(payload)
        assert payload["tools"][0]["max_uses"] == 5

    def test_a_cap_the_caller_set_is_left_alone(self) -> None:
        payload = {"tools": [{**ANTHROPIC, "max_uses": 2}]}
        bound_web_search(payload["tools"], default=5).apply(payload)
        assert payload["tools"][0]["max_uses"] == 2

    def test_nothing_is_written_into_a_tool_that_has_no_such_field(self) -> None:
        """Refusing someone's request to protect a reservation is the wrong trade.

        OpenAI's `web_search` takes `search_context_size` and `filters`, not
        `max_uses`. Injecting one risks a 400 on a request that would have
        worked, so the reservation degrades to a floor and says so.
        """
        payload = {"tools": [dict(OPENAI)]}
        bound = bound_web_search(payload["tools"], default=5)
        bound.apply(payload)
        assert "max_uses" not in payload["tools"][0]
        assert bound.reserved == 5
        assert bound.enforced is False

    def test_two_search_tools_are_summed(self) -> None:
        bound = bound_web_search([{**ANTHROPIC, "max_uses": 2}, ANTHROPIC], default=5)
        assert bound.reserved == 7

    def test_a_foreign_namespace_is_not_our_tool(self) -> None:
        """`openrouter:web_search` is a tool *they* execute. Nothing here does."""
        assert bound_web_search([{"type": "openrouter:web_search"}], default=5).reserved == 0

    @pytest.mark.parametrize("junk", ["tools", 7, {"type": "web_search"}, [None, 3, "x"]])
    def test_junk_is_not_a_crash(self, junk: Any) -> None:
        assert bound_web_search(junk, default=5).reserved == 0

    def test_a_zero_or_negative_cap_falls_back_to_the_default(self) -> None:
        """`max_uses: 0` is not a promise not to search; it is not a valid cap."""
        assert bound_web_search([{**ANTHROPIC, "max_uses": 0}], default=5).reserved == 5
        assert bound_web_search([{**ANTHROPIC, "max_uses": -1}], default=5).reserved == 5


class TestTheLedger:
    async def test_a_reported_search_is_charged_and_counted(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        await price_searches(session, seeded, per_search="0.01")
        fake_upstream.set_json(searched(3))

        response = await client.post(
            "/v1/messages",
            json=messages_body(model=seeded.model.name, tools=[ANTHROPIC]),
            headers=seeded.auth,
        )
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.search_count == 3
        # 100 input at 1/Mtok + 50 output at 2/Mtok + 3 searches at 0.01.
        assert record.cost == Decimal("0.030200")

    async def test_an_unpriced_search_is_counted_and_not_charged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """The gap has to be findable before the provider's invoice arrives.

        No rate means no invented charge — the same rule as an unpriced model —
        but the count is on the row, which is what the report's disclosure and
        a reconciliation are then able to see.
        """
        fake_upstream.set_json(searched(4))

        await client.post(
            "/v1/messages",
            json=messages_body(model=seeded.model.name, tools=[ANTHROPIC]),
            headers=seeded.auth,
        )

        record = await latest_record(session)
        assert record.search_count == 4
        assert record.cost == Decimal("0.000200")

    async def test_a_request_that_did_not_search_records_no_searches(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        await price_searches(session, seeded, per_search="0.01")
        fake_upstream.set_json(
            {
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "no need"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )

        await client.post(
            "/v1/messages",
            json=messages_body(model=seeded.model.name, tools=[ANTHROPIC]),
            headers=seeded.auth,
        )

        record = await latest_record(session)
        assert record.search_count == 0
        assert record.cost == Decimal("0.000200")

    async def test_the_cap_reaches_the_provider(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """End to end: the deployment's default is in the body that was sent."""
        fake_upstream.set_json(searched(1))

        await client.post(
            "/v1/messages",
            json=messages_body(model=seeded.model.name, tools=[ANTHROPIC]),
            headers=seeded.auth,
        )

        sent = fake_upstream.bodies[-1]
        assert sent["tools"][0]["max_uses"] == 5

    async def test_a_caller_who_capped_it_is_not_overridden(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        fake_upstream.set_json(searched(1))

        await client.post(
            "/v1/messages",
            json=messages_body(model=seeded.model.name, tools=[{**ANTHROPIC, "max_uses": 1}]),
            headers=seeded.auth,
        )

        assert fake_upstream.bodies[-1]["tools"][0]["max_uses"] == 1

    async def test_a_request_with_no_tools_gains_none(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """The gateway does not enable search on a request that did not ask."""
        fake_upstream.set_json(searched(0))

        await client.post(
            "/v1/messages",
            json=messages_body(model=seeded.model.name),
            headers=seeded.auth,
        )

        assert "tools" not in fake_upstream.bodies[-1]


class TestTheCeiling:
    """Search spend has to count against a cost limit, or it is not capped.

    Two mechanisms, and it is worth being exact about which does what, because
    the first draft of these tests assumed the wrong one.

    ``max_uses`` written into the outgoing tool is what bounds **this**
    request: the provider refuses the sixth search itself.

    The reservation is what makes those searches count against the window
    **immediately**, so the ceiling engages for what comes next. It cannot
    refuse a request for its own cost — the quota engine subtracts a request's
    own contribution back out before judging, which is its documented overrun
    policy: refused *at* the limit, and an admitted request may overshoot by
    its own usage.
    """

    @staticmethod
    async def tight_cost_rule(session: AsyncSession, seeded: Seeded) -> None:
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            metric=LimitMetric.COST,
            limit="0.04",
            window_seconds=3600,
        )

    async def test_search_spend_closes_the_window_on_later_requests(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """Three searching requests against a 0.04 ceiling: the third is refused.

        Each costs 0.0302 — 0.03 of it searches — so the window passes 0.04
        during the second, and the third is refused on what the window already
        held. Without the search charge the same three requests cost 0.0006 in
        total, which is the control below.
        """
        await price_searches(session, seeded, per_search="0.01")
        await self.tight_cost_rule(session, seeded)
        fake_upstream.set_json(searched(3))

        body = messages_body(model=seeded.model.name, tools=[ANTHROPIC])
        first = await client.post("/v1/messages", json=body, headers=seeded.auth)
        second = await client.post("/v1/messages", json=body, headers=seeded.auth)
        third = await client.post("/v1/messages", json=body, headers=seeded.auth)

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "quota_exceeded"

    async def test_the_same_requests_without_search_all_pass(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """The control: it is the searches that closed the window, not the tokens."""
        await price_searches(session, seeded, per_search="0.01")
        await self.tight_cost_rule(session, seeded)
        fake_upstream.set_json(searched(0))

        body = messages_body(model=seeded.model.name)
        for _ in range(3):
            response = await client.post("/v1/messages", json=body, headers=seeded.auth)
            assert response.status_code == 200

    async def test_an_unpriced_search_reserves_nothing(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """No rate means no ceiling, which is the hazard, stated as a test.

        Exactly the shape of an unpriced model: it reserves nothing, so it has
        no cost ceiling at all. The `search_count` on every row is what makes
        the gap visible — see `TestTheLedger`.
        """
        await self.tight_cost_rule(session, seeded)
        fake_upstream.set_json(searched(50))

        body = messages_body(model=seeded.model.name, tools=[ANTHROPIC])
        for _ in range(3):
            response = await client.post("/v1/messages", json=body, headers=seeded.auth)
            assert response.status_code == 200
