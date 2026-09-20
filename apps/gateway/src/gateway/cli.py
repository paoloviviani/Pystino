"""Command line entry points: ``gateway serve``, ``seed``, ``rebuild-counters``.

``seed`` exists so that ``docker compose up`` yields a system you can actually
send a request to. Without it, a fresh database has no group, no model and no key,
and the first thing anyone tries returns 401 followed by 404.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
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
    LocalCredential,
    Membership,
    MembershipRole,
    MembershipSource,
    ModelDef,
    ModelPrice,
    PriceSource,
    Provider,
    User,
)
from gateway.passwords import hash_password, validate_password
from gateway.security import generate_api_key

logger = logging.getLogger(__name__)

# `--password` with this value (or bare) prompts instead: an actual password on
# argv lands in shell history, which is a leak with a long memory.
_PROMPT_SENTINEL = "-"


async def _seed(
    *,
    group_name: str,
    model_name: str,
    upstream_model: str,
    input_price: Decimal,
    output_price: Decimal,
    email: str,
    password: str = "",
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

            if password:
                # Opt-in: a seeded credential nobody asked for would put a
                # guessable account on the management surface. Given, it is
                # validated against the same policy `passwd` enforces.
                validate_password(password, settings.local_auth)
                credential = await session.get(LocalCredential, user.id)
                if credential is None:
                    session.add(
                        LocalCredential(user_id=user.id, password_hash=hash_password(password))
                    )
                else:
                    credential.password_hash = hash_password(password)

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
    seed.add_argument(
        "--password",
        default="",
        help="also give the seeded user a local email+password login (prompted "
        "for if the flag is given without a value)",
        nargs="?",
        const=_PROMPT_SENTINEL,
    )

    passwd = subparsers.add_parser(
        "passwd",
        help="create or reset a local email+password login (ADR 0043)",
        description=(
            "Creates the local user if needed and sets the password supplied "
            "interactively. This is the bootstrap for the first administrator: "
            "it needs no running gateway and no existing session."
        ),
    )
    passwd.add_argument("email", help="the address to sign in with (subject of the local user)")
    passwd.add_argument(
        "--group",
        default="",
        help="a billing group to add the user to (created if missing; "
        "existing memberships are untouched)",
    )
    passwd.add_argument(
        "--admin",
        # Not a `--admin/--no-admin` string pair: argparse treats that slash
        # form as one literal option name that matches nothing — found when the
        # first `gateway passwd --no-admin` on the live stack failed with
        # "unrecognized arguments". BooleanOptionalAction is what actually
        # registers both spellings.
        action=argparse.BooleanOptionalAction,
        default=True,
        help="whether the account is an administrator (default: yes)",
    )
    passwd.add_argument(
        "--display-name",
        default="",
        help="shown in the console; defaults to none",
    )

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
        # `--password` with no value prompts rather than taking the next
        # argument: a password on argv lands in shell history, which is a leak
        # with a long memory.
        password: str = args.password
        if password == _PROMPT_SENTINEL:
            try:
                password = getpass.getpass(f"Password for {args.email}: ")
            except KeyboardInterrupt:
                # Same rule as passwd's prompts: an abort exits cleanly
                # with 130, not an asyncio traceback under compose exec.
                print("\nAborted.", file=sys.stderr)
                return 130
        secret = asyncio.run(
            _seed(
                group_name=args.group,
                model_name=args.model,
                upstream_model=args.upstream_model,
                input_price=args.input_price,
                output_price=args.output_price,
                email=args.email,
                password=password,
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

    if args.command == "passwd":
        return asyncio.run(
            _passwd(
                email=args.email,
                group=args.group,
                is_admin=args.admin,
                display_name=args.display_name,
            )
        )

    return 1


async def _passwd(
    *, email: str, group: str = "", is_admin: bool = False, display_name: str = ""
) -> int:
    """Create or update a local login (ADR 0043).

    Exists because the first local administrator has to come from somewhere
    that is not itself authenticated: the admin API that would otherwise
    create accounts requires an admin, which on a fresh database nobody is.
    The same chicken-and-egg is why this does not require the gateway to be
    running — it opens its own database session.

    Idempotent by design: running it again for an address resets that
    account's password. That is the point — "the admin lost the password" is
    the recovery story — and it is also why the command asks nothing else:
    anyone with shell access to the host can already read the database.
    """
    settings = get_settings()
    try:
        first = getpass.getpass(f"Password for {email}: ")
        second = getpass.getpass("Again: ")
    except KeyboardInterrupt:
        # Ctrl+C at a password prompt is an abort, not a traceback: this
        # command runs inside `docker compose exec` during installs (the
        # operator's TTY is attached), and an unhandled SIGINT there
        # surfaces as asyncio internals plus a stack the operator cannot
        # act on — or worse, a live process holding the terminal. The
        # installer resumes idempotently, so a clean exit 130 is the
        # honest answer.
        print("\nAborted.", file=sys.stderr)
        return 130
    if first != second:
        print("The two passwords do not match.", file=sys.stderr)
        return 1
    try:
        validate_password(first, settings.local_auth)
    except ValueError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            user = (
                await session.execute(
                    select(User).where(User.issuer == "local", User.subject == email.casefold())
                )
            ).scalar_one_or_none()
            if user is None:
                user = User(
                    issuer="local",
                    subject=email.casefold(),
                    email=email.casefold(),
                    display_name=display_name or None,
                    is_admin=is_admin,
                )
                session.add(user)
                await session.flush()
            else:
                # A reset must not silently re-empower a demoted account: only
                # the flag explicitly passed on the command line touches it.
                user.is_admin = is_admin

            # Memberships are additive here, unlike the OIDC path: there is no
            # directory to be authoritative, so `gateway passwd` adds the group
            # named and says nothing about the others.
            if group:
                grp = (
                    await session.execute(select(Group).where(Group.name == group))
                ).scalar_one_or_none()
                if grp is None:
                    grp = Group(
                        name=group,
                        description="Created by `gateway passwd`",
                        source=GroupSource.MANUAL,
                    )
                    session.add(grp)
                    await session.flush()
                membership = (
                    await session.execute(
                        select(Membership).where(
                            Membership.user_id == user.id, Membership.group_id == grp.id
                        )
                    )
                ).scalar_one_or_none()
                if membership is None:
                    session.add(
                        Membership(
                            user_id=user.id,
                            group_id=grp.id,
                            role=MembershipRole.MEMBER,
                            source=MembershipSource.MANUAL,
                        )
                    )
                if user.default_billing_group_id is None:
                    user.default_billing_group_id = grp.id

            credential = await session.get(LocalCredential, user.id)
            if credential is None:
                session.add(LocalCredential(user_id=user.id, password_hash=hash_password(first)))
            else:
                credential.password_hash = hash_password(first)
            await session.commit()

        print(f"Local sign-in ready for {email} (issuer 'local').")
        if not settings.local_auth.enabled:
            print(
                "Note: GATEWAY_LOCAL_AUTH__ENABLED is false, so the gateway will refuse "
                "password logins until it is set to true and the gateway restarted."
            )
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
