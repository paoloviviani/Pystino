"""The selection rules and the session key, with no database and no HTTP.

Each rule is a pure ordering over candidates, which is what makes them testable
this way, and what lets a decision-model rule or a catalogue-driven one slot in
behind the same interface later (issue P1, P3, P5).
"""

from __future__ import annotations

import random
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from gateway.models import ApiSurface, ModelDef, ModelKind, ModelPrice
from gateway.routing import (
    DEFAULT_RULE,
    RULES,
    Candidate,
    Cheapest,
    FirstAvailable,
    SelectionContext,
    SelectionRule,
    StickyHit,
    Weighted,
    affinity_key,
    needs_write,
    rule_named,
    session_key_hash,
    status_falls_back,
)
from gateway.types import utcnow


def model(name: str, *, price: tuple[str, str, str] | None = ("1", "2", "EUR")) -> ModelDef:
    row = ModelDef(
        id=uuid.uuid4(),
        name=name,
        upstream_model=f"up/{name}",
        provider_id=uuid.uuid4(),
        kind=ModelKind.CHAT,
    )
    row.prices = (
        []
        if price is None
        else [
            ModelPrice(
                input_per_mtok=Decimal(price[0]),
                output_per_mtok=Decimal(price[1]),
                currency=price[2],
                effective_from=utcnow() - timedelta(days=1),
            )
        ]
    )
    return row


def cand(name: str, position: int, weight: int = 1, **kw: Any) -> Candidate:
    return Candidate(target=model(name, **kw), position=position, weight=weight)


def ctx(**kw: Any) -> SelectionContext:
    return SelectionContext(alias=ModelDef(name="alias"), surface=ApiSurface.CHAT_COMPLETIONS, **kw)


def names(ordered: list[Candidate]) -> list[str]:
    return [c.target.name for c in ordered]


class FakeFX:
    """A converter with a fixed USD->EUR rate and nothing else."""

    async def rate(self, base: str, quote: str) -> SimpleNamespace | None:
        if (base.upper(), quote.upper()) == ("USD", "EUR"):
            return SimpleNamespace(rate=Decimal("0.5"))
        return None


class TestFirstAvailable:
    async def test_follows_the_operators_order_whatever_order_it_arrives_in(self) -> None:
        candidates = [cand("c", 2), cand("a", 0), cand("b", 1)]
        assert names(await FirstAvailable().order(candidates, ctx())) == ["a", "b", "c"]

    async def test_is_the_default_and_the_answer_for_an_unknown_name(self) -> None:
        assert rule_named(DEFAULT_RULE).name == "first_available"
        assert rule_named("a_rule_from_a_newer_build").name == "first_available"


class TestWeighted:
    async def test_a_seeded_draw_is_reproducible(self) -> None:
        candidates = [cand("a", 0, 1), cand("b", 1, 1), cand("c", 2, 1)]
        first = names(await Weighted().order(candidates, ctx(rng=random.Random(7))))
        again = names(await Weighted().order(candidates, ctx(rng=random.Random(7))))
        assert first == again
        assert sorted(first) == ["a", "b", "c"]

    async def test_the_first_pick_follows_the_weights(self) -> None:
        candidates = [cand("light", 0, 1), cand("heavy", 1, 9)]
        rng = random.Random(1234)
        wins = {"light": 0, "heavy": 0}
        for _ in range(4000):
            ordered = await Weighted().order(candidates, ctx(rng=rng))
            wins[ordered[0].target.name] += 1
        # 10% expected for the light one: seeded, so this is a fixed number, and
        # the band is wide enough that a different seed would also sit inside it.
        assert 0.07 < wins["light"] / 4000 < 0.13

    async def test_every_candidate_stays_in_the_order_for_fallback(self) -> None:
        candidates = [cand("a", 0, 5), cand("b", 1, 1)]
        for seed in range(20):
            ordered = await Weighted().order(candidates, ctx(rng=random.Random(seed)))
            assert sorted(names(ordered)) == ["a", "b"]


class TestCheapest:
    async def test_orders_by_input_plus_output_list_price(self) -> None:
        candidates = [
            cand("dear", 0, price=("10", "30", "EUR")),
            cand("cheap", 1, price=("1", "2", "EUR")),
            cand("mid", 2, price=("3", "6", "EUR")),
        ]
        assert names(await Cheapest().order(candidates, ctx())) == ["cheap", "mid", "dear"]

    async def test_an_unpriced_target_is_last_not_first(self) -> None:
        candidates = [cand("free?", 0, price=None), cand("priced", 1, price=("5", "5", "EUR"))]
        assert names(await Cheapest().order(candidates, ctx())) == ["priced", "free?"]

    async def test_ties_keep_the_operators_order(self) -> None:
        candidates = [cand("b", 1), cand("a", 0), cand("c", 2)]
        assert names(await Cheapest().order(candidates, ctx())) == ["a", "b", "c"]

    async def test_converts_a_foreign_currency_into_the_billing_currency(self) -> None:
        # 10 USD at 0.5 is 5 EUR, which is under the 8 EUR rival.
        candidates = [
            cand("euro", 0, price=("4", "4", "EUR")),
            cand("dollar", 1, price=("5", "5", "USD")),
        ]
        ordered = await Cheapest().order(candidates, ctx(fx=FakeFX()))  # type: ignore[arg-type]
        assert names(ordered) == ["dollar", "euro"]

    async def test_a_currency_with_no_rate_is_treated_as_unpriced(self) -> None:
        candidates = [
            cand("yen", 0, price=("1", "1", "JPY")),
            cand("euro", 1, price=("9", "9", "EUR")),
        ]
        ordered = await Cheapest().order(candidates, ctx(fx=FakeFX()))  # type: ignore[arg-type]
        assert names(ordered) == ["euro", "yen"]


class TestRegistry:
    def test_the_three_rules_are_registered_under_their_stored_names(self) -> None:
        assert set(RULES) == {"first_available", "weighted", "cheapest"}

    async def test_a_rule_written_elsewhere_satisfies_the_interface(self) -> None:
        """What a decision-model or residency rule will look like: a name and an
        async `order`, free to drop candidates it does not want."""

        class OnlyTheFirst:
            name = "only_the_first"

            async def order(
                self, candidates: list[Candidate], context: SelectionContext
            ) -> list[Candidate]:
                return sorted(candidates, key=lambda c: c.position)[:1]

        rule: SelectionRule = OnlyTheFirst()
        assert isinstance(rule, SelectionRule)
        assert names(await rule.order([cand("b", 1), cand("a", 0)], ctx())) == ["a"]


class TestFallbackPolicy:
    @pytest.mark.parametrize("status", [401, 402, 408, 429, 500, 502, 503, 504, 529, 599])
    def test_provider_side_failures_move_on(self, status: int) -> None:
        assert status_falls_back(status)

    @pytest.mark.parametrize("status", [200, 201, 301, 400, 403, 404, 409, 413, 422])
    def test_the_requests_own_failures_do_not(self, status: int) -> None:
        assert not status_falls_back(status)


class TestForbidden:
    @pytest.mark.parametrize(
        "body",
        [
            {"error": {"message": "Insufficient Balance."}},
            {"error": {"code": "billing_hard_limit_reached"}},
            {"error": {"type": "authentication_error", "message": "x"}},
            {"error": {"message": "You exceeded your current quota"}},
            {"error": "invalid api key"},
        ],
    )
    def test_a_403_about_our_account_falls_back(self, body: dict[str, Any]) -> None:
        assert status_falls_back(403, body)

    @pytest.mark.parametrize(
        "body",
        [None, {}, {"error": {"message": "content policy violation"}}, {"error": {"code": "x"}}],
    )
    def test_any_other_403_is_the_requests(self, body: Any) -> None:
        assert not status_falls_back(403, body)


class TestSessionKey:
    def test_the_explicit_header_wins_over_everything(self) -> None:
        body = SimpleNamespace(model_extra={"prompt_cache_key": "pck", "user": "u"})
        headers = {"x-session-id": "sid", "x-session-affinity": "aff"}
        assert affinity_key(headers, body) == "sid"

    def test_then_the_affinity_header_then_the_cache_key_then_user(self) -> None:
        body = SimpleNamespace(model_extra={"prompt_cache_key": "pck", "user": "u"})
        assert affinity_key({"x-session-affinity": "aff"}, body) == "aff"
        assert affinity_key({}, body) == "pck"
        assert affinity_key({}, SimpleNamespace(model_extra={"user": "u"})) == "u"

    def test_a_declared_field_counts_as_much_as_an_extra(self) -> None:
        assert affinity_key({}, SimpleNamespace(model_extra=None, user="declared")) == "declared"

    def test_blank_values_are_no_key(self) -> None:
        assert (
            affinity_key({"x-session-id": "   "}, SimpleNamespace(model_extra={"user": ""})) is None
        )
        assert affinity_key({}, None) is None

    def test_a_long_key_is_capped(self) -> None:
        assert len(affinity_key({"x-session-id": "x" * 5000}) or "") == 512

    def test_the_stored_hash_is_scoped_to_the_caller_and_hides_the_key(self) -> None:
        alice, bob = uuid.uuid4(), uuid.uuid4()
        assert session_key_hash(alice, "s1") != session_key_hash(bob, "s1")
        assert session_key_hash(alice, "s1") == session_key_hash(alice, "s1")
        assert "s1" not in session_key_hash(alice, "s1")


class TestWhenToWrite:
    def test_a_new_session_or_a_different_target_always_writes(self) -> None:
        now = utcnow()
        target = uuid.uuid4()
        assert needs_write(None, target, 3600, now)
        hit = StickyHit(target_model_id=uuid.uuid4(), expires_at=now + timedelta(seconds=3500))
        assert needs_write(hit, target, 3600, now)

    def test_the_same_target_renews_only_in_the_last_half_of_the_window(self) -> None:
        now = utcnow()
        target = uuid.uuid4()
        fresh = StickyHit(target_model_id=target, expires_at=now + timedelta(seconds=3000))
        ageing = StickyHit(target_model_id=target, expires_at=now + timedelta(seconds=1000))
        assert not needs_write(fresh, target, 3600, now)
        assert needs_write(ageing, target, 3600, now)
