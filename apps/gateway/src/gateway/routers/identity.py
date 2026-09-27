"""``GET /v1/me``: who is calling, and may they administer this deployment.

The door problem ``routers/billing.py`` records, one question further on.
`GET /api/me` has answered "who am I, and am I an administrator" since the
console shipped, but every ``/api`` route is behind ``get_management_user``,
which reads a session cookie and nothing else. A chat client authenticating
with an OIDC access token has no cookie to present, so it could not ask.

What it did instead was call an admin-only route — the knowledge configuration
— and read a 403 as "not an administrator". That is a proxy for the question,
not the question: it answers "may I do this one thing", so every new admin
section needs its own probe, and a route that starts refusing for an unrelated
reason (a disabled feature, a provider outage) silently demotes the person.

**Groups and ``is_admin`` come from effective memberships, never from the
token's ``groups`` claim** (ADR 0057), which is the same rule ``x-bill-to`` and
`/v1/billing/groups` follow. A claim is what the *directory* believes;
``memberships.source`` distinguishes ``oidc`` from ``manual``, and a group an
administrator granted by hand appears in no token. Deriving admin from the
claim is precisely the bug ADR 0057 records: the administrator who was made an
administrator by hand — the escape hatch from the lockout ADR 0056 warns about
— would be told they are not one. Nothing here reads the token at all: the
bearer path has already reconciled memberships and recomputed ``is_admin``
against ``admin_groups`` by the time a principal exists.

**A key never learns that its owner is an administrator.** An issued key is
what a program holds; an access token is evidence a person just signed in.
Every administrative surface in this deployment already acts on that
distinction — ``/api`` needs a session cookie, and the knowledge configuration
refuses a key even when its owner is an admin — so reporting ``is_admin: true``
to a key would promise access the credential cannot exercise: a client would
open an admin panel whose every action is then refused. The worse failure is
the one that decides it: a leaked ``gwk_`` key would be enough to open that
panel in any client trusting the flag, turning a credential a program was
handed into the administrative decision a person is supposed to make by
signing in.

**Which credential is which is provenance, not shape.** The house IdP keeps
its access tokens opaque — ``gwa`` rows in the same ``api_keys`` table, minted
at login (``minted_by`` set, ADR 0046) — so "a key" and "an access token"
cannot be told apart by looking at the bearer. They are told apart by where
the row came from: a *minted* credential is the session's proof and answers
as the signed-in person it stands in for; an *issued* key (``minted_by IS
NULL``) answers as a program's credential, and its flag is false for its
owner's own safety.

So the flag answers for the credential: **false for an issued key, whoever
owns it; the person's own answer for a minted credential**. It is not
*silently* false in the key case — ``credential`` names which credential
answered, so a client seeing ``api_key`` knows to ask again with an access
token rather than concluding the person is not an administrator.

Refusing the key outright was the alternative, and was rejected: it would make
this stricter than `/v1/billing/groups`, which answers for a key today, and
identity is not the privileged part. A program that cannot ask whose key it is
holding gets that answer from a support ticket instead.
"""

from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.deps import AuthenticatedCallerDep, SessionDep, SettingsDep, _bearer_client
from gateway.errors import AuthenticationError, PermissionError_, TooManyRequestsError
from gateway.merges import resolve_merged_from
from gateway.models import User, UserIdentity
from gateway.oidc import OIDCError, ProvisioningRefused, sign_in
from gateway.schemas import CallerIdentity, IdentityRef, MeIdentities, SessionAnnounce
from gateway.security import parse_authorization_header

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["pystino"])


async def _identities_for(session: AsyncSession, user: User) -> list[IdentityRef]:
    """The primary (issuer, subject) plus every linked ``user_identities`` row."""
    refs = [IdentityRef(issuer=user.issuer, subject=user.subject)]
    rows = (
        await session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))
    ).scalars().all()
    refs.extend(IdentityRef(issuer=row.issuer, subject=row.subject) for row in rows)
    return refs


@router.post("/session/announce", response_model=SessionAnnounce)
async def announce(request: Request, session: SessionDep, settings: SettingsDep) -> SessionAnnounce:
    """The chat's sign-in door (ADR 0093 §4.1, §4.3): called with the fresh
    access token its own login callback just received. Runs the full sign-in
    sequence — link, bind, provision, the admin rules, the bootstrap — and
    answers who this is. Never trusts a claim in the request body: there is
    none. The token itself is the only input, and its claims are read only
    after the gateway has validated the signature and fetched userinfo with
    it itself.
    """
    token = parse_authorization_header(request.headers.get("authorization"))
    if not token:
        raise AuthenticationError("Supply the access token as 'Authorization: Bearer <token>'.")

    client, record = await _bearer_client(request, session, token)
    if client is None or record is None:
        raise AuthenticationError("Invalid or unrecognised access token.")

    try:
        claims = await client.validate_access_token(token)
    except OIDCError as exc:
        logger.info("announce: token rejected: %s", exc)
        raise AuthenticationError("Invalid or unrecognised access token.") from exc

    azp = claims.get("azp") or claims.get("client_id")
    if not settings.oidc.chat_client_id or azp != settings.oidc.chat_client_id:
        raise AuthenticationError("This token was not issued to the chat client.")

    issuer = str(claims["iss"])
    subject = str(claims["sub"])
    if not request.app.state.announce_limiter.allow((issuer, subject)):
        raise TooManyRequestsError("Too many announce calls for this identity. Try again shortly.")

    merged: dict[str, object] = dict(claims)
    merged.update(await client.fetch_userinfo(token))

    global_policy = getattr(request.app.state, "oidc_policy", None)
    policy = (
        _dc_replace(global_policy.policy, groups_claim=record.groups_claim)
        if global_policy is not None
        else None
    )

    try:
        result = await sign_in(
            session,
            record,
            issuer=issuer,
            subject=subject,
            claims=merged,
            settings=settings,
            policy=policy,
        )
    except ProvisioningRefused as exc:
        await session.commit()
        raise PermissionError_(str(exc)) from exc
    await session.commit()

    user = result.user
    if not user.is_active:
        raise PermissionError_(
            "This account is not enabled. Ask an administrator to enable it, then sign in again."
        )

    identities = await _identities_for(session, user)
    merged_from = await resolve_merged_from(session, user.id)
    return SessionAnnounce(
        id=user.id,
        identities=identities,
        merged_from=merged_from,
        is_active=user.is_active,
        is_admin=user.is_admin,
        sessions_valid_after=user.sessions_valid_after,
        merged_at=user.merged_at,
    )


@router.get("/me/identities", response_model=MeIdentities)
async def my_identities(caller: AuthenticatedCallerDep, session: SessionDep) -> MeIdentities:
    """Everything that names this person here (ADR 0093 §4.1): the primary
    pair, every linked identity, and every id ever merged into this one, so
    the chat can adopt or fold records it has not keyed on the gateway's id
    yet.

    Access-token callers only, the same ``credential`` distinction
    ``/v1/me`` already draws: a personal API key is a program's credential,
    with no business listing the identities of the person who issued it. A
    *minted* credential (the house IdP's own access tokens, stored as ``gwa``
    rows) answers as the person it is proof of having signed in as, exactly
    like it does for ``is_admin`` there.

    Authenticates via :data:`AuthenticatedCallerDep`, not ``PrincipalDep``
    (ADR 0093 to-do item 2): this is an identity question, not a billing one,
    and must answer for a caller with no billing group at all.
    """
    by_key = caller.api_key is not None and caller.api_key.minted_by is None
    if by_key:
        raise PermissionError_(
            "An API key may not list its owner's identities. Sign in and use an "
            "access token instead."
        )
    user = caller.user
    identities = await _identities_for(session, user)
    merged_from = await resolve_merged_from(session, user.id)
    return MeIdentities(id=user.id, identities=identities, merged_from=merged_from)


@router.get("/me", response_model=CallerIdentity)
async def whoami(caller: AuthenticatedCallerDep) -> CallerIdentity:
    # No query. Authentication already loaded the user with its memberships,
    # and each membership's group comes with it (`lazy="joined"`), so this
    # route adds nothing to the round-trip budget `test_query_counts.py` pins.
    #
    # Authenticates via `AuthenticatedCallerDep`, not `PrincipalDep` (ADR 0093
    # to-do item 2): resolving a billing group is exactly the thing this route
    # must not require — a bundled user with no membership at all still has an
    # identity to report, just no group to bill, and this is the route that
    # tells them so instead of 403ing before they learn anything.
    user = caller.user
    # Provenance, not shape (see the module docstring): a minted credential is
    # a session's proof and answers as its person; an issued key is a program's
    # credential and its owner's admin flag stays theirs to exercise by
    # signing in.
    minted = caller.api_key is not None and caller.api_key.minted_by is not None
    by_key = caller.api_key is not None and not minted

    # Every effective membership, including groups that are disabled. The
    # billable subset is `/v1/billing/groups`' question and it filters them for
    # a reason — `resolve_billing_group` refuses a disabled group, so offering
    # one in a billing dropdown offers a choice that fails on use. Here the
    # opposite holds: a disabled group still grants model access and still
    # confers admin through `admin_groups`, so hiding it would make this list
    # disagree with both `/v1/models` and the flag beside it.
    groups = sorted(
        group.name
        for membership in user.memberships
        if (group := membership.group) is not None
    )
    default = next(
        (
            group.name
            for membership in user.memberships
            if (group := membership.group) is not None
            and group.id == user.default_billing_group_id
        ),
        None,
    )

    return CallerIdentity(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        # The credential's answer, not the person's. See the module docstring:
        # a key that could report `true` would let a stolen program credential
        # open an admin panel no administrative route would then serve.
        is_admin=user.is_admin and not by_key,
        credential="api_key" if by_key else "access_token",
        groups=groups,
        default_billing_group=default,
        # The same pinned-or-default group a metered request honouring
        # `x-bill-to` would charge, resolved by `resolve_billing_group_or_none`
        # instead of `resolve_billing_group` — null when nothing resolves,
        # which the raising version would have 403'd on (ADR 0093 to-do
        # item 2).
        billing_group=caller.billing_group.name if caller.billing_group else None,
        sessions_valid_after=user.sessions_valid_after,
        merged_at=user.merged_at,
    )
