"""Command line entry points: ``gateway serve``, ``seed``, ``rebuild-counters``.

``seed`` exists so that ``docker compose up`` yields a system you can actually
send a request to. Without it, a fresh database has no group, no model and no key,
and the first thing anyone tries returns 401 followed by 404.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from sqlalchemy import select

from gateway.config import get_settings
from gateway.db import create_engine, create_session_factory
from gateway.logging_config import configure_logging
from gateway.models import (
    ApiKey,
    Group,
    GroupModelAccess,
    GroupSource,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    MembershipRole,
    MembershipSource,
    ModelDef,
    ModelPrice,
    PriceSource,
    Provider,
    User,
)
from gateway.security import generate_api_key

logger = logging.getLogger(__name__)


async def _seed(
    *,
    group_name: str,
    model_name: str,
    upstream_model: str,
    input_price: Decimal,
    output_price: Decimal,
    email: str,
) -> str:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)

    try:
        async with factory() as session:
            group = (
                await session.execute(select(Group).where(Group.name == group_name))
            ).scalar_one_or_none()
            if group is None:
                group = Group(
                    name=group_name,
                    description="Created by `gateway seed`",
                    source=GroupSource.MANUAL,
                )
                session.add(group)
                await session.flush()

            # Migration 0003 creates this from GATEWAY_UPSTREAM__*, so it is
            # normally already here; seeding a fresh database created with
            # create_all rather than alembic still needs it.
            provider = (
                await session.execute(select(Provider).where(Provider.name == "default"))
            ).scalar_one_or_none()
            if provider is None:
                provider = Provider(
                    name="default",
                    description="Created by `gateway seed`",
                    base_url=settings.upstream.base_url,
                )
                session.add(provider)
                await session.flush()

            model = (
                await session.execute(select(ModelDef).where(ModelDef.name == model_name))
            ).scalar_one_or_none()
            if model is None:
                model = ModelDef(
                    name=model_name,
                    upstream_model=upstream_model,
                    provider_id=provider.id,
                    display_name=model_name,
                )
                session.add(model)
                await session.flush()
                session.add(
                    ModelPrice(
                        model_id=model.id,
                        input_per_mtok=input_price,
                        output_per_mtok=output_price,
                        currency=settings.billing_currency,
                        source=PriceSource.MANUAL,
                    )
                )

            access = (
                await session.execute(
                    select(GroupModelAccess).where(
                        GroupModelAccess.group_id == group.id,
                        GroupModelAccess.model_id == model.id,
                    )
                )
            ).scalar_one_or_none()
            if access is None:
                session.add(GroupModelAccess(group_id=group.id, model_id=model.id))

            # A local user with a fixed subject, so re-seeding is idempotent.
            #
            # Casefolded, because the local login casefolds what it is given
            # and `passwd` casefolds what it stores: a subject seeded as typed
            # produced an account that could never sign in with its password.
            # A deployment that already seeded a mixed-case address gets a
            # *second* row here rather than a repair of the first — accepted
            # deliberately (ADR 0056), as the alternative is a lookup that
            # matches either spelling forever to rescue an address nobody
            # should have typed that way.
            seed_subject = email.casefold()
            user = (
                await session.execute(
                    select(User).where(User.issuer == "local", User.subject == seed_subject)
                )
            ).scalar_one_or_none()
            if user is None:
                user = User(
                    issuer="local",
                    subject=seed_subject,
                    email=seed_subject,
                    display_name="Seed user",
                    is_admin=True,
                )
                session.add(user)
                await session.flush()

            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.user_id == user.id, Membership.group_id == group.id
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                session.add(
                    Membership(
                        user_id=user.id,
                        group_id=group.id,
                        role=MembershipRole.ADMIN,
                        source=MembershipSource.MANUAL,
                    )
                )

            user.default_billing_group_id = group.id

            generated = generate_api_key(environment_prefix=settings.api_key_prefix)
            session.add(
                ApiKey(
                    user_id=user.id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    name="seed key",
                    billing_group_id=group.id,
                )
            )

            # A generous default ceiling, so the demo cannot run away with money
            # but also will not refuse a first request.
            existing_rule = (
                await session.execute(
                    select(LimitRule).where(
                        LimitRule.scope == LimitScope.GROUP,
                        LimitRule.scope_id == group.id,
                        LimitRule.metric == LimitMetric.COST,
                        LimitRule.window_seconds == 86_400,
                    )
                )
            ).scalar_one_or_none()
            if existing_rule is None:
                session.add(
                    LimitRule(
                        name=f"{group_name} daily cost ceiling",
                        scope=LimitScope.GROUP,
                        scope_id=group.id,
                        metric=LimitMetric.COST,
                        window_seconds=86_400,
                        limit_value=Decimal("10"),
                    )
                )

            await session.commit()
            return generated.secret
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the HTTP server")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "worker processes. The gateway is stateless, so scale with processes "
            "rather than threads: the GIL makes threads pointless for this workload."
        ),
    )
    serve.add_argument("--reload", action="store_true")

    seed = subparsers.add_parser("seed", help="create a demo group, model, user and key")
    seed.add_argument("--group", default="research")
    seed.add_argument("--model", default="demo-model")
    seed.add_argument(
        "--upstream-model",
        default="gpt-4o-mini",
        help="the model id sent to the configured upstream",
    )
    seed.add_argument("--input-price", type=Decimal, default=Decimal("0.15"))
    seed.add_argument("--output-price", type=Decimal, default=Decimal("0.60"))
    seed.add_argument("--email", default="seed@example.org")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "gateway.main:app",
            host=args.host,
            port=args.port,
            workers=args.workers if not args.reload else 1,
            reload=args.reload,
            # Our own JSON formatter is already installed by create_app().
            log_config=None,
        )
        return 0

    if args.command == "seed":
        secret = asyncio.run(
            _seed(
                group_name=args.group,
                model_name=args.model,
                upstream_model=args.upstream_model,
                input_price=args.input_price,
                output_price=args.output_price,
                email=args.email,
            )
        )
        print("Seeded. API key (shown once):\n")
        print(f"  {secret}\n")
        print("Try it:\n")
        print(
            f"  curl -s localhost:8000/v1/chat/completions \\\n"
            f'    -H "Authorization: Bearer {secret}" \\\n'
            f'    -H "content-type: application/json" \\\n'
            f'    -d \'{{"model":"{args.model}",'
            f'"messages":[{{"role":"user","content":"hello"}}]}}\'\n'
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
