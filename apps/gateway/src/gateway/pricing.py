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
from gateway.models import ModelDef, ModelPrice, PriceSource
from gateway.types import utcnow

logger = logging.getLogger(__name__)

DEFAULT_CATALOGUE_URL = "https://api.cortecs.ai/v1/models"

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

        context = _first(entry, ("context_length", "context_window", "max_context"))
        try:
            context_window = int(context) if context is not None else None
        except (TypeError, ValueError):
            context_window = None

        prices.append(
            CataloguePrice(
                model_id=model_id,
                input_per_mtok=input_rate,
                output_per_mtok=output_rate,
                currency=str(currency).upper(),
                cache_read_per_mtok=_as_decimal(_first(source, _CACHE_READ_KEYS)),
                cache_write_per_mtok=_as_decimal(_first(source, _CACHE_WRITE_KEYS)),
                context_window=context_window,
            )
        )

    return prices, unparsable


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
