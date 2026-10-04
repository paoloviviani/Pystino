#!/usr/bin/env python
"""Import per-model prices from the Cortecs catalogue.

A separate script rather than a background job inside the gateway: pricing changes
are a deliberate administrative act, and running it on a timer inside the request
path would mean a provider's catalogue could silently change what the organisation
charges its own groups.

    # see what would change, without writing anything (the default)
    uv run python scripts/import_cortecs_pricing.py

    # actually write the new price rows
    uv run python scripts/import_cortecs_pricing.py --apply

    # from a saved response, e.g. to review a change before applying it
    uv run python scripts/import_cortecs_pricing.py --from-file catalogue.json --apply

Only models that already exist in our catalogue get prices. This never creates
models: which models exist, and which groups may reach them, stays an
administrative decision.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
import orjson
from gateway.config import get_settings
from gateway.db import create_engine, create_session_factory
from gateway.logging_config import configure_logging
from gateway.pricing import (
    DEFAULT_CATALOGUE_URL,
    CatalogueUnavailable,
    fetch_catalogue,
    import_prices,
    parse_catalogue,
)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=False)

    if args.from_file:
        payload = orjson.loads(Path(args.from_file).read_bytes())
    else:
        api_key = args.api_key or settings.upstream.api_key.get_secret_value() or None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                payload = await fetch_catalogue(client, args.url, api_key)
        except CatalogueUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    prices, unparsable = parse_catalogue(payload)
    if not prices:
        print(
            "error: no prices could be read from the catalogue. Save the response "
            "with --save-to and check its shape against gateway.pricing.",
            file=sys.stderr,
        )
        if args.save_to:
            Path(args.save_to).write_bytes(orjson.dumps(payload))
        return 2

    if args.save_to:
        Path(args.save_to).write_bytes(orjson.dumps(payload))

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            report = await import_prices(
                session,
                prices,
                billing_currency=settings.billing_currency,
                dry_run=not args.apply,
            )
    finally:
        await engine.dispose()

    report.unparsable.extend(unparsable)

    mode = "would write" if not args.apply else "wrote"
    print(f"{len(prices)} price(s) read from the catalogue.")
    print(f"{mode}: {report.summary()}")

    for name in report.created:
        print(f"  {'+' if args.apply else '~'} {name}")
    if report.currency_mismatches:
        print(f"\nSkipped — priced in another currency (we bill in {settings.billing_currency}):")
        for model_id, currency in report.currency_mismatches:
            print(f"  ! {model_id} is priced in {currency}")
        print(
            "  Refused rather than converted: a silent exchange rate produces "
            "invoices that look correct and are not."
        )
    if report.unknown_models:
        shown = report.unknown_models[:10]
        print(f"\nNot in our catalogue ({len(report.unknown_models)}), so not priced:")
        for model_id in shown:
            print(f"  - {model_id}")
        if len(report.unknown_models) > len(shown):
            print(f"  ... and {len(report.unknown_models) - len(shown)} more")
    if report.unparsable:
        print(f"\nCould not be parsed ({len(report.unparsable)}):")
        for model_id in report.unparsable[:10]:
            print(f"  ? {model_id}")

    if not args.apply and report.created:
        print("\nNothing was written. Re-run with --apply to write these prices.")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_CATALOGUE_URL)
    parser.add_argument(
        "--api-key",
        default=None,
        help="defaults to GATEWAY_UPSTREAM__API_KEY; optional for this endpoint",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the price rows. Without it, nothing is written.",
    )
    parser.add_argument("--from-file", default=None, help="read a saved response instead")
    parser.add_argument("--save-to", default=None, help="save the raw response here")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
