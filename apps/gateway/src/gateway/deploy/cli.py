"""The `pystino` command line: bootstrap, release-pin, admin, break-glass,
idp check, email export-env, erasure list/retry, quota health.

Every command takes its answers as flags so CI and scripts never meet a prompt.

Writing `.env` and standing up a deployment is no longer this CLI's job
(ADR 0091): `deploy/.env.example` in this repository (a Pystino-only
deployment) and the separate `cerea-deploy` repository (the full stack, with
its own `./configure`) are what an operator reads and edits directly. What
stays here runs *inside* a deployment: `bootstrap` (the compose one-shot on
every `up`), `admin grant|revoke` (the ordinary-case recovery) and
`break-glass` (the deeper one, ADR 0093 §10 — run by `./configure
--break-glass` after it has already rewritten `.env` to the bundled
Authelia), `idp check` (a live probe against the configured identity
provider, ADR 0093 §11), `email export-env` (the mail configuration in
force, for `./configure --import-smtp`, ADR 0093 §13.5) and `erasure
list|retry` (the chat erasure queue the background retry loop already owns,
for an operator who wants to see or force it from the host, ADR 0093 §9.3),
plus `release-pin`, which a release of this repository runs against its own
checkout.
"""

from __future__ import annotations

import argparse
import subprocess
import uuid
from pathlib import Path

from gateway.deploy import bootstrap, envfile, idp_check

#: This file is apps/gateway/src/gateway/deploy/cli.py; the repository root
#: (holding deploy/release.env) is five levels up.
ROOT = Path(__file__).resolve().parents[5]


def cmd_bootstrap(args: argparse.Namespace) -> int:
    return bootstrap.run()


def cmd_admin(args: argparse.Namespace) -> int:
    import asyncio

    from gateway.config import get_settings
    from gateway.db import create_engine, create_session_factory
    from gateway.deploy.admin import AdminCommandError, set_admin

    async def run() -> int:
        engine = create_engine(get_settings())
        try:
            async with create_session_factory(engine)() as session:
                user = await set_admin(
                    session, args.email, grant=args.action == "grant", issuer=args.issuer or ""
                )
        except AdminCommandError as exc:
            print(f"admin: {exc}")
            return 1
        finally:
            await engine.dispose()
        state = "is now" if user.is_admin else "is no longer"
        print(f"{user.email} ({user.issuer}) {state} an administrator")
        return 0

    return asyncio.run(run())


def cmd_break_glass(args: argparse.Namespace) -> int:
    import asyncio

    from gateway.config import get_settings
    from gateway.db import create_engine, create_session_factory
    from gateway.deploy.admin import AdminCommandError, break_glass
    from gateway.secrets import SecretBox

    try:
        user_id = uuid.UUID(args.user_id) if args.user_id else None
    except ValueError:
        print(f"break-glass: --user-id must be a UUID, got {args.user_id!r}")
        return 2

    async def run() -> int:
        settings = get_settings()
        engine = create_engine(settings)
        secrets = SecretBox(settings.secret_key_list())
        try:
            async with create_session_factory(engine)() as session:
                result = await break_glass(
                    session,
                    settings,
                    secrets,
                    email=args.email,
                    login=args.login,
                    user_id=user_id,
                    reason=args.reason,
                )
        except AdminCommandError as exc:
            print(f"break-glass: {exc}")
            return 1
        finally:
            await engine.dispose()
        # Printed once, to stdout only: never through `logging`, so it never
        # reaches a log sink (ADR 0093 §10 step 5).
        print(f"login:    {result.login}")
        print(f"password: {result.password}")
        return 0

    return asyncio.run(run())


def cmd_email_export_env(args: argparse.Namespace) -> int:
    """The mail configuration in force, as `KEY=VALUE` lines (ADR 0093 §13.5).

    For `cerea-deploy`'s `./configure --import-smtp`: a deployment carrying
    an old console-set row (from before mail became one environment
    setting) exports it once, including the password — which nothing else
    ever returns in plain text — so an operator moving to `SMTP_*` in `.env`
    does not have to already know it or reset it.
    """
    import asyncio

    from gateway.config import get_settings
    from gateway.db import create_engine, create_session_factory
    from gateway.email_config import effective_smtp
    from gateway.secrets import SecretBox

    async def run() -> int:
        settings = get_settings()
        engine = create_engine(settings)
        secrets = SecretBox(settings.secret_key_list())
        try:
            async with create_session_factory(engine)() as session:
                effective = await effective_smtp(session, settings, secrets)
        finally:
            await engine.dispose()
        for key, value in (
            ("SMTP_HOST", effective.host),
            ("SMTP_PORT", str(effective.port)),
            ("SMTP_USERNAME", effective.username),
            ("SMTP_PASSWORD", effective.password),
            ("SMTP_FROM", effective.from_address),
            ("SMTP_SECURITY", effective.security),
        ):
            print(f"{key}={value}")
        return 0

    return asyncio.run(run())


def cmd_erasure_list(args: argparse.Namespace) -> int:
    """Every pending chat erasure (ADR 0093 §9.3): what the retry loop has,
    from the host, for an operator who wants to see the queue without a
    database client."""
    import asyncio

    from sqlalchemy import select

    from gateway.config import get_settings
    from gateway.db import create_engine, create_session_factory
    from gateway.models import ChatErasure, ChatErasureStatus

    async def run() -> int:
        engine = create_engine(get_settings())
        try:
            async with create_session_factory(engine)() as session:
                rows = (
                    await session.execute(
                        select(ChatErasure)
                        .where(ChatErasure.status == ChatErasureStatus.PENDING)
                        .order_by(ChatErasure.created_at)
                    )
                ).scalars().all()
        finally:
            await engine.dispose()
        if not rows:
            print("no erasures pending")
            return 0
        for row in rows:
            due = row.next_attempt_at.isoformat() if row.next_attempt_at else "now"
            print(
                f"{row.id}  user={row.gateway_user_id}  attempts={row.attempts}  "
                f"next={due}  last_error={row.last_error or '-'}"
            )
        return 0

    return asyncio.run(run())


def cmd_erasure_retry(args: argparse.Namespace) -> int:
    """Force one pending erasure's next attempt now, ignoring its backoff —
    for an operator who has just fixed the reason it was failing and does
    not want to wait out the schedule."""
    import asyncio

    from gateway.config import get_settings
    from gateway.db import create_engine, create_session_factory
    from gateway.erasure import attempt_erasure
    from gateway.models import ChatErasure, ChatErasureStatus

    async def run() -> int:
        settings = get_settings()
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                row = await session.get(ChatErasure, uuid.UUID(args.erasure_id))
                if row is None:
                    print(f"no erasure with id {args.erasure_id}")
                    return 1
                if row.status is ChatErasureStatus.DONE:
                    print(f"{row.id} is already done")
                    return 0
                import httpx

                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
                    done = await attempt_erasure(session, settings, http, row)
        finally:
            await engine.dispose()
        print(f"{args.erasure_id}: {'done' if done else 'still pending, see last_error'}")
        return 0 if done else 1

    return asyncio.run(run())


def cmd_quota_health(args: argparse.Namespace) -> int:
    """Quota counters against the ledger, as JSON on stdout (the same figures as
    ``GET /api/admin/quota/health``, without a browser session).

    Read-only, and for ``cerea-deploy``'s ``tools/diagnose``, which runs it with
    ``docker compose exec gateway`` so an operator can attach the result to a
    report. Prints no secret: only rule names, ids and figures.
    """
    import asyncio
    import json
    from decimal import Decimal

    from gateway.config import get_settings
    from gateway.db import create_engine, create_session_factory
    from gateway.quota import DatabaseCounterStore, InMemoryCounterStore, QuotaEngine
    from gateway.quota.counters import ValkeyCounterStore

    def plain(value: Decimal | None) -> str | None:
        # Plain digits, never "1E-9": money is a string end to end.
        return None if value is None else format(value, "f")

    async def run() -> int:
        settings = get_settings()
        engine = create_engine(settings)
        client = None
        try:
            store: object = InMemoryCounterStore()
            if settings.valkey_url:
                import redis.asyncio as redis_asyncio

                client = redis_asyncio.from_url(settings.valkey_url, decode_responses=True)
                store = ValkeyCounterStore(client)
            factory = create_session_factory(engine)
            quota = QuotaEngine(
                store,  # type: ignore[arg-type]
                settings=settings.quota,
                fallback=DatabaseCounterStore(factory),
                billing_timezone=settings.billing_timezone,
            )
            async with factory() as session:
                health = await quota.health(session)
        finally:
            if client is not None:
                await client.aclose()
            await engine.dispose()
        print(
            json.dumps(
                {
                    "windows": [
                        {
                            "rule": w.rule.name,
                            "rule_id": str(w.rule.id),
                            "scope": w.rule.scope.value,
                            "scope_id": str(w.rule.scope_id) if w.rule.scope_id else None,
                            "metric": w.rule.metric.value,
                            "window": w.rule.window_label,
                            "window_id": w.window_id,
                            "active": w.rule.is_active,
                            "counter": plain(w.counter),
                            "ledger": plain(w.ledger),
                            "difference": plain(w.difference),
                            "counter_ttl_seconds": w.counter_ttl_seconds,
                            "stale_in_progress": w.stale_in_progress,
                        }
                        for w in health.windows
                    ],
                    "rebuild_lock_present": health.lock_present,
                    "rebuild_lock_ttl_seconds": health.lock_ttl_seconds,
                },
                indent=2,
            )
        )
        return 0

    return asyncio.run(run())


def cmd_release_pin(args: argparse.Namespace) -> int:
    from gateway.deploy import release

    path = Path(args.manifest) if args.manifest else ROOT / "deploy" / "release.env"
    if args.check:
        missing = release.unpinned(envfile.read(path))
        for key in missing:
            print(f"unpinned: {key}")
        return 1 if missing else 0
    try:
        changes = release.pin_file(path)
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        print(f"release-pin: {exc} (is this host logged in to the registry?)")
        return 1
    for key, value in changes.items():
        print(f"{key}={value}")
    print(f"pinned {len(changes)} image(s) in {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pystino", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap", help="(inside the stack) converge DB and IdP state")
    boot.set_defaults(func=cmd_bootstrap)

    rp = sub.add_parser("release-pin", help="pin the release manifest's images by digest")
    rp.add_argument("--manifest", help="default: this checkout's deploy/release.env")
    rp.add_argument("--check", action="store_true", help="fail if any image is unpinned")
    rp.set_defaults(func=cmd_release_pin)

    adm = sub.add_parser("admin", help="(inside the gateway) ordinary-case admin grant/revoke")
    adm.add_argument("action", choices=("grant", "revoke"))
    adm.add_argument("email")
    adm.add_argument("--issuer", help="when the email names accounts at several issuers")
    adm.set_defaults(func=cmd_admin)

    bg = sub.add_parser(
        "break-glass", help="(inside the gateway) recover admin access to the bundled Authelia"
    )
    bg.add_argument("--email", required=True, help="the target account's address")
    bg.add_argument("--login", help="the bundled login name; default: derived from the email")
    bg.add_argument("--user-id", help="disambiguate when --email names several accounts")
    bg.add_argument("--reason", required=True, help="why break-glass, for the audit row")
    bg.set_defaults(func=cmd_break_glass)

    idp = sub.add_parser("idp", help="identity provider diagnostics")
    idp_sub = idp.add_subparsers(dest="idp_command", required=True)
    check = idp_sub.add_parser("check", help="probe the configured identity provider live")
    idp_check.build_arg_parser(check)
    check.set_defaults(func=idp_check.cmd_idp_check)

    email = sub.add_parser("email", help="mail configuration")
    email_sub = email.add_subparsers(dest="email_command", required=True)
    export_env = email_sub.add_parser(
        "export-env", help="the mail configuration in force, as KEY=VALUE lines"
    )
    export_env.set_defaults(func=cmd_email_export_env)

    erasure = sub.add_parser("erasure", help="the chat erasure queue (ADR 0093 §9.3)")
    erasure_sub = erasure.add_subparsers(dest="erasure_command", required=True)
    erasure_list = erasure_sub.add_parser("list", help="every pending chat erasure")
    erasure_list.set_defaults(func=cmd_erasure_list)
    erasure_retry = erasure_sub.add_parser("retry", help="force one erasure's next attempt now")
    erasure_retry.add_argument("erasure_id")
    erasure_retry.set_defaults(func=cmd_erasure_retry)

    quota = sub.add_parser("quota", help="quota counters")
    quota_sub = quota.add_subparsers(dest="quota_command", required=True)
    quota_health = quota_sub.add_parser(
        "health", help="each rule's counter against the ledger, as JSON (read-only)"
    )
    quota_health.set_defaults(func=cmd_quota_health)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
