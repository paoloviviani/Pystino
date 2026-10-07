"""``pystino idp check`` (ADR 0093 §11): a live probe against the configured
identity provider.

Diagnostic only, never on the request path, and read-only: it answers two
questions a login itself never says out loud. Does discovery actually agree
with what ``.env`` claims — the issuer, the endpoints, whether the key count
is what an operator expects? And, handed a real sign-in (a pasted access
token, or one earned live through the device flow), would any admin rule
actually grant it, and to which of the two reasons?

Defaults to the running deployment's own ``Settings``, so
``./configure --check`` can run ``--discovery-only`` unmodified against the
candidate ``.env`` before anything else is up, and an operator can run the
rest once the stack is live.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.config import OIDCSettings, Settings
from gateway.email_normalize import is_trusted_email, normalize_email
from gateway.oidc import (
    OIDCClient,
    OIDCError,
    _matches_claim_rule,
    _matches_email_rule,
    resolve_claim,
)


@dataclass
class CheckReport:
    """Lines to print, and whether the check as a whole passed.

    A single flat list rather than a structured tree: the CLI's only job is
    to print this in order, and a report a test can assert on line-by-line is
    more useful than one it has to walk.
    """

    lines: list[str] = field(default_factory=list)
    ok: bool = True

    def say(self, line: str) -> None:
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.lines.append(line)
        self.ok = False


async def check_discovery(client: OIDCClient, settings: OIDCSettings, report: CheckReport) -> bool:
    """Discovery and JWKS. Returns whether both succeeded."""
    try:
        metadata = await client.metadata()
    except OIDCError as exc:
        report.fail(f"discovery: FAILED — {exc}")
        return False

    configured = settings.issuer.rstrip("/")
    published = metadata.issuer.rstrip("/")
    if published == configured:
        report.say(f"discovery: issuer {metadata.issuer!r} (matches OIDC_ISSUER)")
    else:
        report.fail(
            f"discovery: issuer {metadata.issuer!r} does NOT match OIDC_ISSUER {settings.issuer!r}"
        )
    report.say(f"  authorization_endpoint: {metadata.authorization_endpoint}")
    report.say(f"  token_endpoint: {metadata.token_endpoint}")
    report.say(f"  userinfo_endpoint: {metadata.userinfo_endpoint or '(none published)'}")
    report.say(
        "  device_authorization_endpoint: "
        f"{metadata.device_authorization_endpoint or '(none published)'}"
    )
    report.say(
        "  end_session_endpoint: "
        f"{metadata.end_session_endpoint or '(none published — see OIDC_LOGOUT_URL)'}"
    )

    try:
        keys = await client.jwks()
    except OIDCError as exc:
        report.fail(f"  jwks: FAILED — {exc}")
        return False
    report.say(f"  jwks: {len(keys.keys)} key(s)")
    return True


def _admin_rule_lines(
    claims: dict[str, Any], settings: OIDCSettings, group_mappings: dict[str, str] | None
) -> tuple[list[str], bool]:
    """Per-rule "would grant admin" lines, and whether any rule granted."""
    lines: list[str] = []
    granted = False

    email_rule_configured = bool(settings.admin_email_list())
    if email_rule_configured:
        matched = _matches_email_rule(claims, settings)
        granted = granted or matched
        if matched:
            lines.append("admin (email rule): yes — email is verified and in ADMIN_EMAILS")
        else:
            email = claims.get("email")
            verified = claims.get("email_verified")
            if verified is not True:
                lines.append(
                    f"admin (email rule): no — email_verified is {verified!r} "
                    f"({type(verified).__name__}), not the JSON boolean true"
                )
            elif not isinstance(email, str) or not is_trusted_email(email)[1]:
                lines.append(f"admin (email rule): no — {email!r} is not a trusted address")
            else:
                lines.append(
                    f"admin (email rule): no — {normalize_email(email)!r} is not in ADMIN_EMAILS"
                )

    claim_rule_configured = bool(settings.admin_claim and settings.admin_claim_value_list())
    if claim_rule_configured:
        matched = _matches_claim_rule(claims, settings, group_mappings)
        granted = granted or matched
        value = resolve_claim(claims, settings.admin_claim)
        if matched:
            lines.append(
                f"admin (claim rule): yes — {settings.admin_claim!r} is {value!r}, "
                "matching ADMIN_CLAIM_VALUES"
            )
        else:
            lines.append(
                f"admin (claim rule): no — {settings.admin_claim!r} is {value!r}, "
                "matching none of ADMIN_CLAIM_VALUES"
            )

    if not email_rule_configured and not claim_rule_configured:
        lines.append(
            "admin: no rule is configured (set ADMIN_EMAILS, or ADMIN_CLAIM and ADMIN_CLAIM_VALUES)"
        )
    return lines, granted


async def check_token(
    client: OIDCClient,
    access_token: str,
    settings: OIDCSettings,
    report: CheckReport,
    *,
    group_mappings: dict[str, str] | None = None,
    count_link_candidates: Any = None,
) -> None:
    """The report built from a real access token: sub, claims, admin rules.

    ``client.validate_access_token`` already checks signature, issuer,
    audience and (once ``ACCEPTED_CLIENTS`` is set) ``azp`` — exactly the
    checks a live sign-in is subject to — so its own message, on failure, is
    the diagnostic: which audiences the token held, what was expected.
    """
    try:
        claims = await client.validate_access_token(access_token)
    except OIDCError as exc:
        report.fail(f"token: FAILED — {exc}")
        return
    report.say(f"token: valid — sub={claims.get('sub')!r}")

    email = claims.get("email")
    verified = claims.get("email_verified")
    report.say(f"  email: {email!r}, email_verified: {verified!r} ({type(verified).__name__})")
    groups_value = resolve_claim(claims, settings.groups_claim)
    report.say(f"  groups claim {settings.groups_claim!r}: {groups_value!r}")
    aud = claims.get("aud")
    report.say(f"  aud: {aud!r} (OIDC_AUDIENCE={settings.access_token_audience!r})")
    azp = claims.get("azp") or claims.get("client_id")
    accepted = settings.accepted_client_list()
    if accepted:
        report.say(
            f"  azp/client_id: {azp!r} "
            f"({'accepted' if azp in accepted else 'NOT in ACCEPTED_CLIENTS'})"
        )
    else:
        report.say(f"  azp/client_id: {azp!r} (ACCEPTED_CLIENTS is not set — any client passes)")

    lines, granted = _admin_rule_lines(claims, settings, group_mappings)
    for line in lines:
        report.say(f"  {line}")
    if not granted:
        report.fail(
            "  no admin rule would grant for this sign-in — check ADMIN_EMAILS / "
            "ADMIN_CLAIM+ADMIN_CLAIM_VALUES above, or that email_verified is the "
            "literal boolean true"
        )

    if settings.link_by_email:
        if count_link_candidates is None:
            report.say("  link-by-email is on, but no database was reached to count candidates")
        elif not isinstance(email, str) or not email:
            report.say("  link-by-email is on, but this sign-in carries no email to match")
        else:
            count = await count_link_candidates(email)
            report.say(f"  link-by-email is on: would link to {count} existing candidate(s)")


async def device_flow(
    client: OIDCClient, *, client_id: str, report: CheckReport, echo: Any = print
) -> str | None:
    """RFC 8628, driven to completion. Returns the access token, or ``None``."""
    try:
        started = await client.start_device_flow(client_id=client_id)
    except OIDCError as exc:
        report.fail(f"device flow: FAILED to start — {exc}")
        return None

    verification = started.get("verification_uri_complete") or started["verification_uri"]
    if "verification_uri_complete" in started:
        echo(f"Open {verification} to sign in.")
    else:
        echo(f"Open {verification} and enter code: {started['user_code']}")

    interval = float(started.get("interval", 5))
    deadline = time.monotonic() + float(started.get("expires_in", 600))
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            result = await client.poll_device_token(
                device_code=started["device_code"], client_id=client_id
            )
        except OIDCError as exc:
            report.fail(f"device flow: FAILED — {exc}")
            return None
        pending = result.get("pending")
        if pending == "slow_down":
            interval += 5
            continue
        if pending == "authorization_pending":
            continue
        token = result.get("access_token")
        if not isinstance(token, str):
            report.fail("device flow: token response carried no access_token")
            return None
        return token

    report.fail("device flow: timed out waiting for sign-in")
    return None


async def run_check(
    settings: Settings,
    *,
    issuer: str | None = None,
    internal_base_url: str | None = None,
    discovery_only: bool = False,
    device: bool = False,
    token: str | None = None,
    client_id: str | None = None,
    http: httpx.AsyncClient | None = None,
    group_mappings: dict[str, str] | None = None,
    count_link_candidates: Any = None,
    echo: Any = print,
) -> CheckReport:
    """The whole check, independent of argument parsing or process exit codes.

    Kept separate from ``cmd_idp_check`` so a test drives it with a fake
    ``httpx.AsyncClient`` transport and a fake device-flow issuer, without an
    ``argparse.Namespace`` or a real event loop's stdout in the way.
    """
    report = CheckReport()
    oidc = settings.oidc
    overrides: dict[str, Any] = {}
    if issuer:
        overrides["issuer"] = issuer
    if internal_base_url is not None:
        overrides["internal_base_url"] = internal_base_url
    if overrides:
        oidc = oidc.model_copy(update=overrides)

    owns_http = http is None
    http = http or httpx.AsyncClient(timeout=10.0)
    client = OIDCClient(oidc, http)
    try:
        if not await check_discovery(client, oidc, report):
            return report
        if discovery_only:
            return report

        access_token = token
        if device:
            machine_client_id = client_id or ""
            if not machine_client_id:
                report.fail("--device needs a client id (OIDC_MACHINE_CLIENT_ID, or --client-id)")
                return report
            access_token = await device_flow(
                client, client_id=machine_client_id, report=report, echo=echo
            )
            if access_token is None:
                return report
        if access_token is None:
            report.say("no --token or --device given: discovery only")
            return report

        await check_token(
            client,
            access_token,
            oidc,
            report,
            group_mappings=group_mappings,
            count_link_candidates=count_link_candidates,
        )
    finally:
        if owns_http:
            await http.aclose()
    return report


def build_arg_parser(parser: argparse.ArgumentParser) -> None:
    """Add ``idp check``'s own flags to a subparser this CLI already created."""
    parser.add_argument("--issuer", help="override OIDC_ISSUER for this check")
    parser.add_argument("--internal-base-url", help="override OIDC_INTERNAL_BASE_URL")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--discovery-only", action="store_true", help="stop after discovery and JWKS")
    mode.add_argument("--device", action="store_true", help="earn a token live via the device flow")
    mode.add_argument("--token", help="check a pasted access token instead of signing in")
    parser.add_argument(
        "--client-id", help="the device flow's client id (default: OIDC_MACHINE_CLIENT_ID)"
    )


async def _fetch_group_mappings(settings: Settings) -> dict[str, str]:
    """The console's group mappings (ADR 0093 §3.4), read directly from
    ``oidc_config`` rather than through a running app's poller — this runs
    as a one-shot CLI command, not a worker."""
    from gateway.db import create_engine, create_session_factory
    from gateway.oidc_policy import OIDCPolicyResolver

    engine = create_engine(settings)
    try:
        resolver = OIDCPolicyResolver(settings.oidc, create_session_factory(engine))
        await resolver.refresh_once()
        return dict(resolver.policy.group_mappings)
    finally:
        await engine.dispose()


async def _count_link_candidates(settings: Settings, email: str) -> int:
    from sqlalchemy import func, select

    from gateway.db import create_engine, create_session_factory
    from gateway.models import User

    engine = create_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            normalized = normalize_email(email)
            count = await session.scalar(
                select(func.count()).select_from(User).where(User.email_normalized == normalized)
            )
            return int(count or 0)
    finally:
        await engine.dispose()


def cmd_idp_check(args: argparse.Namespace) -> int:
    from gateway.config import get_settings

    async def count_candidates(email: str) -> int:
        return await _count_link_candidates(get_settings(), email)

    async def main() -> int:
        settings = get_settings()
        client_id = args.client_id or _env_machine_client_id()

        group_mappings: dict[str, str] | None = None
        count_link_candidates = None
        # A database is only worth reaching once a real sign-in is being
        # checked — --discovery-only runs before the stack necessarily has
        # one (./configure --check, against a candidate .env).
        if not args.discovery_only and (args.device or args.token):
            group_mappings = await _fetch_group_mappings(settings)
            count_link_candidates = count_candidates

        report = await run_check(
            settings,
            issuer=args.issuer,
            internal_base_url=args.internal_base_url,
            discovery_only=args.discovery_only,
            device=args.device,
            token=args.token,
            client_id=client_id,
            group_mappings=group_mappings,
            count_link_candidates=count_link_candidates,
        )
        for line in report.lines:
            print(line)
        if not report.ok:
            print("idp check: FAILED")
        return 0 if report.ok else 1

    return asyncio.run(main())


def _env_machine_client_id() -> str:
    """``OIDC_MACHINE_CLIENT_ID``, read raw: it names the device flow's own
    client, which is a compose/``./configure`` fact, not a gateway
    ``Settings`` field — the gateway only ever checks the resulting token's
    ``azp`` against ``ACCEPTED_CLIENTS``, never mints as this client itself.
    """
    import os

    return os.environ.get("OIDC_MACHINE_CLIENT_ID", "")
