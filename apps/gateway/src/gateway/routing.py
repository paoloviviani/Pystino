"""Routing policies for aliases with more than one target (issue P1).

An alias (ADR 0101) names a set of interchangeable models. This module decides,
for one request, **in what order they are tried**. It does not call anything,
bill anything or read the request body: it is a pure ordering over candidates
the caller already loaded, plus the small store that remembers where a session
went. What happens with the order (admission per attempt, the upstream call,
falling back) lives in ``routers/_metered.py``.

**The seam for later work is** :class:`SelectionRule`. A rule is given every
*available* target and returns them in preference order; the first is tried, the
rest are the fallback order. That one shape is deliberately all a rule gets:

* a **decision model** (issue P3) is a rule that asks a hosted decider and
  returns ``[decided, *the others]``. It is async for that reason;
* a **catalogue-driven** rule (issue P5: region, retention, residency) is a rule
  that *removes* candidates whose metadata does not qualify, and orders what is
  left. Returning fewer candidates than it was given is part of the contract;
* the three rules here are the same thing with nothing to ask.

Rules never see quota, health or stickiness. Those are applied around them
(stickiness moves the remembered target to the front; admission and failure
move on to the next), so a rule written later cannot forget to honour them.

**The session key** (:func:`affinity_key`) is the first of, in order: the
``x-session-id`` header, the ``x-session-affinity`` header, the body's
``prompt_cache_key``, the body's ``user``. No key means no stickiness for that
request, not a shared bucket.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.accounting.cost import select_price
from gateway.catalogue import CatalogueFacts
from gateway.fx import FXService
from gateway.models import ApiSurface, ModelDef, RouteSession
from gateway.types import utcnow

#: At most this many targets are tried for one request. Every failed attempt
#: writes a ledger row and holds a reservation for the length of the call, so an
#: alias with a dozen targets must not turn one outage into a dozen of each.
MAX_ATTEMPTS = 3

#: The rule an alias gets unless told otherwise, and the one an unknown name
#: (a row written by a newer build, then rolled back) is treated as.
DEFAULT_RULE = "first_available"

#: Headers that carry an explicit session id, in priority order. ``x-session-id``
#: is ours; ``x-session-affinity`` is the name coding agents use for the same
#: idea, so they stick without configuration.
SESSION_HEADERS = ("x-session-id", "x-session-affinity")
#: Body fields that identify a conversation or a person, in priority order.
#: ``prompt_cache_key`` is OpenAI's own affinity key and is a per-conversation
#: id by design; ``user`` is the end-user id, coarser but better than nothing.
SESSION_BODY_FIELDS = ("prompt_cache_key", "user")

_MAX_KEY_CHARS = 512


# -- falling back --------------------------------------------------------------

#: Which upstream HTTP statuses move a request on to the next target.
#:
#: 5xx, because the provider is failing and another may not be. 429, because the
#: provider is refusing *us* (our key's rate limit or credit) and is no verdict
#: on the request. 408 is a timeout the provider reports. 401 and 402 as well:
#: the caller already authenticated to this gateway, so an upstream 401 or 402
#: means *our* credential or balance at that provider is bad (one provider
#: answers "Insufficient Balance." with a 401). 403 is ambiguous (a content
#: policy is the request's fault, a suspended account is ours), so it falls back
#: only when the body says auth, billing or quota (:func:`refusal_is_ours`).
#: Every other 4xx (400, 404, 413, 422 ...) is what the **request** did wrong: a
#: second target would be asked the same question and either refuse it too,
#: doubling the spend on a bad request, or accept something the first correctly
#: refused.
FALLBACK_STATUSES = frozenset({401, 402, 408, 429})

#: Words in a 403 body that say the refusal is about our account, not the request.
_OURS = ("auth", "billing", "quota", "balance", "credit", "api key", "api_key", "subscription")


def refusal_is_ours(content: Any, message: str | None = None) -> bool:
    """Whether a 403 body says authentication, billing or quota.

    Reads the provider's ``error`` object (``code``, ``type``, ``message``) or a
    plain message; anything that mentions none of the words stays the request's
    fault.
    """
    parts: list[str] = [message or ""]
    if isinstance(content, dict):
        error = content.get("error", content)
        if isinstance(error, dict):
            parts += [str(error.get(k) or "") for k in ("code", "type", "message")]
        elif isinstance(error, str):
            parts.append(error)
    text = " ".join(parts).lower()
    return any(word in text for word in _OURS)


def status_falls_back(status_code: int, content: Any = None, message: str | None = None) -> bool:
    """Whether an upstream answer with this status moves on to the next target."""
    if status_code in FALLBACK_STATUSES or 500 <= status_code <= 599:
        return True
    return status_code == 403 and refusal_is_ours(content, message)


# -- the selection interface ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One target an alias may be served by, already known to be available."""

    target: ModelDef
    position: int
    weight: int = 1
    #: What the provider catalogue says about the provider behind this target
    #: (backlog P4), or ``None`` when the provider is not linked to an entry (or
    #: the caller has no catalogue, a unit test). For a residency or
    #: zero-data-retention rule to filter on; a field that is ``None`` inside is
    #: *unknown*, which a rule must decide how to treat.
    facts: CatalogueFacts | None = None

    @property
    def region(self) -> str | None:
        return self.facts.region if self.facts else None

    @property
    def zero_data_retention(self) -> bool | None:
        return self.facts.zero_data_retention if self.facts else None

    @property
    def retention_days(self) -> int | None:
        return self.facts.retention_days if self.facts else None


@dataclass(slots=True)
class SelectionContext:
    """What a rule may look at. Read-only by convention."""

    alias: ModelDef
    surface: ApiSurface
    #: The caller's session key, if they sent one. For a rule that wants it; the
    #: sticky memory is applied by the planner, not by rules.
    session_key: str | None = None
    #: The billing currency, and a way to convert into it, for rules that compare
    #: prices. ``None`` when the caller has no converter (a unit test).
    currency: str = "EUR"
    fx: FXService | None = None
    #: Injected so that a weighted draw is reproducible in a test.
    rng: random.Random = field(default_factory=random.Random)


@runtime_checkable
class SelectionRule(Protocol):
    """Order the available targets of an alias for one request.

    ``name`` is what ``models.route_rule`` stores. ``order`` returns the
    candidates in the order to try them; it may return fewer than it received
    (a rule that excludes) but never a candidate it was not given, and an empty
    answer means "nothing qualifies" and fails the request as unavailable.
    """

    name: str

    async def order(
        self, candidates: Sequence[Candidate], context: SelectionContext
    ) -> list[Candidate]: ...


class FirstAvailable:
    """The operator's order, as written. The first available target serves."""

    name = "first_available"

    async def order(
        self, candidates: Sequence[Candidate], context: SelectionContext
    ) -> list[Candidate]:
        return sorted(candidates, key=lambda c: c.position)


class Weighted:
    """A weighted draw per request; the draw also fixes the fallback order.

    Weighted sampling without replacement (each target's key is
    ``random() ** (1 / weight)``, largest first), so the first target is chosen
    with probability ``weight / total`` and the next with that probability among
    what remains. Falling back therefore keeps the shape the operator set.
    """

    name = "weighted"

    async def order(
        self, candidates: Sequence[Candidate], context: SelectionContext
    ) -> list[Candidate]:
        def key(candidate: Candidate) -> float:
            weight = max(candidate.weight, 1)
            return float(context.rng.random() ** (1.0 / weight))

        keyed = [(key(c), c) for c in sorted(candidates, key=lambda c: c.position)]
        return [c for _, c in sorted(keyed, key=lambda pair: pair[0], reverse=True)]


def list_price(
    model: ModelDef, input_weight: int = 1, output_weight: int = 1
) -> tuple[Decimal, str] | None:
    """The one number ``cheapest`` compares, with its currency.

    Input rate * ``input_weight`` plus output rate * ``output_weight``, per
    million tokens (the alias's weights; 1:1 adds them, 10:1 suits long prompts
    with short answers); or, for a model priced by the image or by the page
    instead, that rate, which the weights do not touch. Cache and search rates are left out:
    they depend on the request, and this is a ranking of list prices, not an
    estimate of a bill. ``None`` for a model with no price in force.
    """
    price = select_price(list(model.prices))
    if price is None:
        return None
    tokens = price.input_per_mtok * input_weight + price.output_per_mtok * output_weight
    if tokens > 0:
        return tokens, price.currency
    for unit in (price.per_image, price.per_page, price.per_minute):
        if unit is not None and unit > 0:
            return unit, price.currency
    return None


class Cheapest:
    """Lowest list price first; the rest in price order, ties by the operator's order.

    A target with **no price** sorts last, never first: an unpriced model reserves
    nothing and bills nothing (AGENTS.md), so ranking it cheapest would send every
    request to the one target the gateway cannot account for. Prices in another
    currency are converted into the billing currency at today's rate; a pair with
    no rate is treated as unpriced rather than guessed.
    """

    name = "cheapest"

    async def order(
        self, candidates: Sequence[Candidate], context: SelectionContext
    ) -> list[Candidate]:
        ranked: list[tuple[Decimal | None, int, Candidate]] = []
        for candidate in candidates:
            ranked.append(
                (await self._cost(candidate.target, context), candidate.position, candidate)
            )
        # None last, then ascending cost, then the operator's order.
        ranked.sort(key=lambda item: (item[0] is None, item[0] or Decimal(0), item[1]))
        return [candidate for _, _, candidate in ranked]

    @staticmethod
    async def _cost(model: ModelDef, context: SelectionContext) -> Decimal | None:
        # `or 1`: an alias row not yet flushed has no column defaults applied.
        found = list_price(
            model,
            context.alias.route_price_input_weight or 1,
            context.alias.route_price_output_weight or 1,
        )
        if found is None:
            return None
        amount, currency = found
        if currency.upper() == context.currency.upper():
            return amount
        if context.fx is None:
            return None
        quote = await context.fx.rate(currency, context.currency)
        return None if quote is None else amount * quote.rate


#: The rules an alias may use, by stored name. A later rule registers here and
#: appears in the admin API's validation with no other change.
RULES: dict[str, SelectionRule] = {
    rule.name: rule for rule in (FirstAvailable(), Weighted(), Cheapest())
}


def rule_named(name: str) -> SelectionRule:
    """The rule stored under ``name``; an unknown name is the default, never an error.

    A request must not fail because a row names a rule this build lacks (a
    rollback after an admin chose a newer one). The default is the operator's own
    order, which is always a defined behaviour.
    """
    return RULES.get(name) or RULES[DEFAULT_RULE]


# -- the session key -----------------------------------------------------------


def affinity_key(headers: Mapping[str, str], body: Any = None) -> str | None:
    """The session identifier a request carries, or ``None``.

    See the module docstring for the order. A value is trimmed and capped; an
    empty one is no key. ``body`` is the validated request model (or ``None``);
    its declared fields and its extras are both read, because which of them a
    schema declares differs by surface.
    """
    for header in SESSION_HEADERS:
        value = headers.get(header)
        if value and value.strip():
            return value.strip()[:_MAX_KEY_CHARS]
    if body is not None:
        extra = getattr(body, "model_extra", None) or {}
        for name in SESSION_BODY_FIELDS:
            value = extra.get(name, getattr(body, name, None))
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_KEY_CHARS]
    return None


def session_key_hash(user_id: uuid.UUID, key: str) -> str:
    """What is stored for a session: a digest scoped to the caller.

    The caller's id is in the digest so that two people who happen to send the
    same session id (``1``, ``default``) do not share a target, and so nobody can
    read or steer another person's memory by guessing an id.
    """
    return hashlib.sha256(f"{user_id}\x00{key}".encode()).hexdigest()


# -- the session memory --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StickyHit:
    target_model_id: uuid.UUID
    expires_at: datetime


async def recall(
    session: AsyncSession, alias_id: uuid.UUID, key_hash: str, *, now: datetime | None = None
) -> StickyHit | None:
    """Where this session last went, if the memory has not expired."""
    moment = now or utcnow()
    row = (
        await session.execute(
            select(RouteSession.target_model_id, RouteSession.expires_at).where(
                RouteSession.alias_id == alias_id,
                RouteSession.key_hash == key_hash,
                RouteSession.expires_at > moment,
            )
        )
    ).first()
    return None if row is None else StickyHit(target_model_id=row[0], expires_at=row[1])


def needs_write(
    hit: StickyHit | None, served_id: uuid.UUID, ttl_seconds: int, now: datetime
) -> bool:
    """Whether serving from ``served_id`` must touch the memory.

    A different target always does (that is re-sticking). The same target
    renews only once less than half the window is left, so a busy session costs
    a write per half-TTL instead of one per request.
    """
    if hit is None or hit.target_model_id != served_id:
        return True
    return hit.expires_at - now < timedelta(seconds=ttl_seconds) / 2


async def remember(
    session: AsyncSession,
    alias_id: uuid.UUID,
    key_hash: str,
    target_model_id: uuid.UUID,
    ttl_seconds: int,
    *,
    now: datetime | None = None,
) -> None:
    """Pin this session to ``target_model_id`` for ``ttl_seconds`` from now.

    An update, then an insert if there was nothing to update. The insert runs in
    a savepoint because two first requests of one session can race: the loser
    hits the primary key and falls back to the update, rather than rolling back
    the whole request's session. Expired rows of this alias are swept on the way,
    which is the only deletion this table ever needs.
    """
    moment = now or utcnow()
    expires = moment + timedelta(seconds=ttl_seconds)
    await session.execute(
        delete(RouteSession).where(
            RouteSession.alias_id == alias_id, RouteSession.expires_at <= moment
        )
    )
    values = {"target_model_id": target_model_id, "expires_at": expires, "updated_at": moment}
    where = (RouteSession.alias_id == alias_id, RouteSession.key_hash == key_hash)
    updated = await session.execute(update(RouteSession).where(*where).values(**values))
    if updated.rowcount:  # type: ignore[attr-defined]
        await session.commit()
        return
    try:
        async with session.begin_nested():
            session.add(RouteSession(alias_id=alias_id, key_hash=key_hash, **values))
    except IntegrityError:
        await session.execute(update(RouteSession).where(*where).values(**values))
    await session.commit()
