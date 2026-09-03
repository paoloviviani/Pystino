"""Importing per-model prices from the Cortecs catalogue.

The endpoint is ``GET https://api.cortecs.ai/v1/models`` (auth optional; supplying
a key narrows the result to what your account can reach). Per model it returns a
pricing object with ``input_token`` and ``output_token`` as cost **per million
tokens**, a ``currency``, and optionally ``cache_read_cost`` / ``cache_write_cost``
on the same per-million basis.

Two deliberate design points:

* **Prices are append-only and effective-dated.** An import never mutates an
  existing row, so the cost already recorded against past requests can never be
  rewritten. A new row is written only when the price has actually changed —
  otherwise a nightly cron would append an identical row every night and make the
  history unreadable.
* **Currency mismatches are refused, not converted.** A model priced in USD when
  the gateway bills in EUR is skipped and reported. Applying an exchange rate here
  would produce invoices that look right and are wrong.

The *envelope* around the model list was not verified against a live response
while this was written, only the pricing fields were. The parser therefore accepts
the plausible shapes (``{"data": [...]}``, ``{"models": [...]}``, or a bare list)
and reports anything it cannot read rather than guessing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.accounting.cost import select_price
from gateway.models import ModelDef, ModelKind, ModelPrice, PriceSource
from gateway.types import utcnow

logger = logging.getLogger(__name__)

DEFAULT_CATALOGUE_URL = "https://api.cortecs.ai/v1/models"

#: LiteLLM's community-maintained price file (MIT), the source for the
#: first-party APIs that publish no pricing of their own (ADR 0053).
LITELLM_CATALOGUE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Keys the catalogue might plausibly use, in order of preference.
_LIST_KEYS = ("data", "models", "items")
_ID_KEYS = ("id", "name", "model", "model_id")
_PRICING_KEYS = ("pricing", "prices", "price")
_INPUT_KEYS = ("input_token", "input", "prompt", "input_per_mtok", "input_cost")
_OUTPUT_KEYS = ("output_token", "output", "completion", "output_per_mtok", "output_cost")
_CACHE_READ_KEYS = ("cache_read_cost", "cache_read", "cache_read_input_token")
_CACHE_WRITE_KEYS = ("cache_write_cost", "cache_write", "cache_write_input_token")


class CatalogueUnavailable(Exception):
    """The provider's catalogue could not be fetched or parsed."""


async def fetch_catalogue(client: httpx.AsyncClient, url: str, api_key: str | None = None) -> Any:
    """Fetch a provider catalogue.

    Takes the client rather than making one, so callers pass the *control-plane*
    client with a finite read timeout. Using the upstream streaming client here
    would inherit ``read=None`` and let a hung provider hang an admin page
    indefinitely.
    """
    headers = {"accept": "application/json"}
    if api_key:
        # Optional for Cortecs; supplying it narrows the catalogue to what this
        # account can actually reach, which is what you want to price.
        headers["authorization"] = f"Bearer {api_key}"
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CatalogueUnavailable(f"could not fetch {url}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class CataloguePrice:
    """One model's prices, per million tokens, as published."""

    model_id: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    currency: str
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None
    context_window: int | None = None
    # Chat unless the catalogue says otherwise. Cortecs reports it in
    # `output_modalities`; a catalogue that says nothing gets the safe default,
    # since every model catalogued before this existed was a chat model.
    kind: ModelKind = ModelKind.CHAT
    # What the model takes, produces and can do, as the catalogue reports it
    # (ADR 0031). Empty means "the catalogue did not say", not "cannot".
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    supported_features: tuple[str, ...] = ()


@dataclass
class ImportReport:
    """What an import did. Everything skipped is named, never silently dropped."""

    created: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    unknown_models: list[str] = field(default_factory=list)
    currency_mismatches: list[tuple[str, str]] = field(default_factory=list)
    unparsable: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{len(self.created)} price(s) written",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.unknown_models:
            parts.append(f"{len(self.unknown_models)} not in our catalogue")
        if self.currency_mismatches:
            parts.append(f"{len(self.currency_mismatches)} wrong currency")
        if self.unparsable:
            parts.append(f"{len(self.unparsable)} unparsable")
        return ", ".join(parts)


def _first(mapping: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _as_decimal(value: Any) -> Decimal | None:
    """Parse a published price without ever going through float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        # str() first: Decimal(0.15) is not 0.15.
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed >= 0 else None


def parse_openrouter_catalogue(payload: Any) -> tuple[list[CataloguePrice], list[str]]:
    """OpenRouter's public catalogue: prices are strings of USD *per token*.

    Verified against the live endpoint on 2026-09-03: ``GET
    https://openrouter.ai/api/v1/models`` is public (no key), returns every
    model with ``pricing.prompt`` / ``pricing.completion`` /
    ``pricing.input_cache_read`` as decimal strings of USD per token, plus
    ``context_length`` and ``architecture.input/output_modalities``.

    The generic parser cannot read this: its keys would match (``prompt`` is in
    ``_INPUT_KEYS``) and then treat 0.0000025 as €2.50 per *million* tokens —
    exactly the "off by a factor of a million" failure the generic parser's own
    docstring warns about. The x1,000,000 here is the conversion the unit
    implies, not a currency conversion; the currency is USD because that is
    what OpenRouter quotes.
    """

    scale = Decimal(1_000_000)
    prices: list[CataloguePrice] = []
    unparsable: list[str] = []

    entries: Iterable[Any]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        entries = payload["data"]
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []

    for entry in entries:
        if not isinstance(entry, dict):
            unparsable.append("<non-object entry>")
            continue
        model_id = entry.get("id")
        pricing = entry.get("pricing")
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            unparsable.append(str(model_id) if model_id else "<unidentified model>")
            continue

        input_rate = _as_decimal(pricing.get("prompt"))
        output_rate = _as_decimal(pricing.get("completion"))
        if input_rate is None or output_rate is None:
            # Free models report 0 and import fine; a missing field is a
            # non-text model (image generators have no token pricing) and is
            # named rather than silently dropped.
            unparsable.append(model_id)
            continue

        architecture = entry.get("architecture")
        arch = architecture if isinstance(architecture, dict) else {}

        prices.append(
            CataloguePrice(
                model_id=model_id,
                input_per_mtok=input_rate * scale,
                output_per_mtok=output_rate * scale,
                currency="USD",
                cache_read_per_mtok=(
                    cache_read * scale
                    if (cache_read := _as_decimal(pricing.get("input_cache_read"))) is not None
                    else None
                ),
                context_window=(
                    int(entry["context_length"]) if entry.get("context_length") else None
                ),
                input_modalities=_string_list(arch.get("input_modalities")),
                output_modalities=_string_list(arch.get("output_modalities")),
            )
        )

    return prices, unparsable


def parse_litellm_catalogue(
    payload: Any,
    providers: Sequence[str] | None = None,
) -> tuple[list[CataloguePrice], list[str]]:
    """LiteLLM's community-maintained price file: the source for the first-party
    APIs that do not publish one.

    Verified against the live file on 2026-09-03: entries carry
    ``input_cost_per_token`` / ``output_cost_per_token`` in USD **per token**,
    ``cache_read_input_token_cost`` where applicable, ``context_length``,
    ``mode`` and a ``litellm_provider`` tag. 3,500+ entries, covering every
    first-party API this gateway has a plugin for — OpenAI, Anthropic, Mistral
    and Nebius publish model lists but **no pricing API at all**, and this file
    is the pragmatic source the ecosystem converges on. It is MIT-licensed
    (licence-compatible, ADR 0001) and community-maintained, so it is an
    *import source with a review step* — the console's discovery-then-import
    flow — and not a trusted authority: a wrong figure here lands in the price
    history exactly as a hand-typed one would, and the append-only rows make
    both visible.

    ``providers`` filters on ``litellm_provider`` (e.g. ``anthropic``,
    ``mistral``, ``nebius``, ``openai``). Entry keys are also the upstream
    model ids in the provider's own namespace, which is what the import
    matches against.
    """

    scale = Decimal(1_000_000)
    wanted = {name.strip().lower() for name in providers} if providers else None

    prices: list[CataloguePrice] = []
    unparsable: list[str] = []

    entries: Iterable[Any]
    if isinstance(payload, dict):
        entries = [
            {**entry, "_id": key}
            for key, entry in payload.items()
            if isinstance(entry, dict)
        ]
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []

    for entry in entries:
        if not isinstance(entry, dict):
            unparsable.append("<non-object entry>")
            continue
        model_id = entry.get("_id") or _first(entry, _ID_KEYS)
        provider_tag = str(entry.get("litellm_provider", "")).lower()
        if wanted is not None and provider_tag not in wanted:
            continue

        input_rate = _as_decimal(entry.get("input_cost_per_token"))
        output_rate = _as_decimal(entry.get("output_cost_per_token"))
        if not model_id or input_rate is None or output_rate is None:
            unparsable.append(str(model_id) or "<unidentified model>")
            continue

        # `mode` maps to what the model produces: "chat" and "completion" are
        # chat models; "embedding" is an embedding model. Everything else
        # (audio, moderation) is imported as chat and deactivated by hand —
        # refused instead, because guessing an image model into existence is
        # how a route starts serving the wrong kind of endpoint.
        mode = str(entry.get("mode", "chat")).lower()
        kind = (
            ModelKind.EMBEDDING
            if mode == "embedding"
            else ModelKind.CHAT
        )

        prices.append(
            CataloguePrice(
                model_id=str(model_id),
                input_per_mtok=input_rate * scale,
                output_per_mtok=output_rate * scale,
                currency="USD",
                cache_read_per_mtok=(
                    cache_read * scale
                    if (
                        cache_read := _as_decimal(entry.get("cache_read_input_token_cost"))
                    ) is not None
                    else None
                ),
                context_window=(
                    int(entry["max_input_tokens"])
                    if entry.get("max_input_tokens")
                    else (
                        int(entry["max_tokens"]) if entry.get("max_tokens") else None
                    )
                ),
                kind=kind,
            )
        )

    return prices, unparsable


def parse_catalogue(payload: Any) -> tuple[list[CataloguePrice], list[str]]:
    """Extract prices from a catalogue response.

    Returns ``(prices, unparsable_ids)``. Entries missing a usable id or price are
    reported rather than skipped quietly, because a model that silently fails to
    import looks exactly like a free model.
    """
    entries: Iterable[Any]
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        found = _first(payload, _LIST_KEYS)
        entries = found if isinstance(found, list) else []
    else:
        entries = []

    prices: list[CataloguePrice] = []
    unparsable: list[str] = []

    for entry in entries:
        if not isinstance(entry, dict):
            unparsable.append("<non-object entry>")
            continue

        raw_id = _first(entry, _ID_KEYS)
        model_id = str(raw_id) if raw_id is not None else None

        pricing = _first(entry, _PRICING_KEYS)
        # Some catalogues put prices at the top level rather than nested.
        source = pricing if isinstance(pricing, dict) else entry

        input_rate = _as_decimal(_first(source, _INPUT_KEYS))
        output_rate = _as_decimal(_first(source, _OUTPUT_KEYS))
        currency = _first(source, ("currency",)) or _first(entry, ("currency",))

        if not model_id or input_rate is None or output_rate is None or not currency:
            unparsable.append(model_id or "<unidentified model>")
            continue

        kind = _kind_of(entry)

        # `context_size` is what the reference provider actually sends, and its
        # absence from this list is why every imported model had a null context
        # window until now. The other spellings stay for catalogues that use
        # them.
        context = _first(entry, ("context_size", "context_length", "context_window", "max_context"))
        try:
            context_window = int(context) if context is not None else None
        except (TypeError, ValueError):
            context_window = None

        prices.append(
            CataloguePrice(
                model_id=model_id,
                input_modalities=_string_list(entry.get("input_modalities")),
                output_modalities=_string_list(entry.get("output_modalities")),
                supported_features=_string_list(entry.get("supported_features")),
                input_per_mtok=input_rate,
                output_per_mtok=output_rate,
                currency=str(currency).upper(),
                cache_read_per_mtok=_as_decimal(_first(source, _CACHE_READ_KEYS)),
                cache_write_per_mtok=_as_decimal(_first(source, _CACHE_WRITE_KEYS)),
                context_window=context_window,
                kind=kind,
            )
        )

    return prices, unparsable


def _string_list(value: Any) -> tuple[str, ...]:
    """A catalogue's string array, normalised and deduplicated.

    Lower-cased and sorted so that two imports of the same model produce the
    same value, and a re-import is therefore visibly a no-op rather than a
    change nobody made.
    """
    if not isinstance(value, list):
        return ()
    seen = {str(item).strip().lower() for item in value if str(item).strip()}
    return tuple(sorted(seen))


def _kind_of(entry: dict[str, Any]) -> ModelKind:
    """What a catalogue entry says the model produces.

    Cortecs derives `output_modalities` from its model tags, so an embedding
    model reports `embeddings` there and an image model reports `image`. The
    name check is a fallback for catalogues that say nothing: "embed" in a
    model id is a strong enough signal to be worth using, and getting it wrong
    only means the model is refused on the wrong route with a message naming
    the fix.

    No name fallback for images. "image" appears in the id of plenty of models
    that *accept* images and generate text, and mislabelling one of those would
    take it off the chat route entirely — a worse failure than leaving an image
    model to be labelled by hand.
    """
    modalities = entry.get("output_modalities")
    if isinstance(modalities, list):
        lowered = [str(item).lower() for item in modalities]
        if any(item.startswith("embed") for item in lowered):
            return ModelKind.EMBEDDING
        if any(item.startswith("image") for item in lowered):
            return ModelKind.IMAGE
        if lowered:
            return ModelKind.CHAT

    identifier = str(_first(entry, _ID_KEYS) or "").lower()
    return ModelKind.EMBEDDING if "embed" in identifier else ModelKind.CHAT


def _differs(existing: ModelPrice | None, candidate: CataloguePrice) -> bool:
    """Whether *candidate* is a genuine change from the price now in force."""
    if existing is None:
        return True
    return (
        existing.input_per_mtok != candidate.input_per_mtok
        or existing.output_per_mtok != candidate.output_per_mtok
        or existing.cache_read_per_mtok != candidate.cache_read_per_mtok
        or existing.cache_write_per_mtok != candidate.cache_write_per_mtok
        or existing.currency.upper() != candidate.currency
    )


async def import_prices(
    session: AsyncSession,
    prices: Sequence[CataloguePrice],
    *,
    billing_currency: str,
    dry_run: bool = True,
    match_on_upstream_model: bool = True,
) -> ImportReport:
    """Write new price rows for models we already know about.

    Models absent from our catalogue are **not** created. Which models exist and
    which groups may reach them is an administrative decision; a pricing importer
    that silently added 56 models to the catalogue would make that decision by
    accident.
    """
    report = ImportReport()
    if not prices:
        return report

    catalogue = list((await session.execute(select(ModelDef))).scalars().all())
    by_name: dict[str, ModelDef] = {known.name: known for known in catalogue}
    if match_on_upstream_model:
        # The published id is the *upstream* model name, which is what our
        # `upstream_model` column holds; `name` is ours to choose.
        for known in catalogue:
            by_name.setdefault(known.upstream_model, known)

    for candidate in prices:
        model = by_name.get(candidate.model_id)
        if model is None:
            report.unknown_models.append(candidate.model_id)
            continue

        if candidate.currency != billing_currency.upper():
            report.currency_mismatches.append((candidate.model_id, candidate.currency))
            continue

        existing_rows = list(
            (await session.execute(select(ModelPrice).where(ModelPrice.model_id == model.id)))
            .scalars()
            .all()
        )
        current = select_price(existing_rows)

        if not _differs(current, candidate):
            report.unchanged.append(model.name)
            continue

        report.created.append(model.name)
        if dry_run:
            continue

        session.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=candidate.input_per_mtok,
                output_per_mtok=candidate.output_per_mtok,
                cache_read_per_mtok=candidate.cache_read_per_mtok,
                cache_write_per_mtok=candidate.cache_write_per_mtok,
                currency=candidate.currency,
                effective_from=utcnow(),
                source=PriceSource.CORTECS,
            )
        )
        if candidate.context_window and not model.context_window:
            model.context_window = candidate.context_window

    if not dry_run:
        await session.commit()

    return report
