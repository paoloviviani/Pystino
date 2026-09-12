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

from fastapi import APIRouter

from gateway.deps import PrincipalDep
from gateway.schemas import CallerIdentity

router = APIRouter(prefix="/v1", tags=["pystino"])


@router.get("/me", response_model=CallerIdentity)
async def whoami(principal: PrincipalDep) -> CallerIdentity:
    # No query. Authentication already loaded the user with its memberships —
    # resolving the billing group is impossible without them — and each
    # membership's group comes with it (`lazy="joined"`), so this route adds
    # nothing to the round-trip budget `test_query_counts.py` pins.
    user = principal.user
    # Provenance, not shape (see the module docstring): a minted credential is
    # a session's proof and answers as its person; an issued key is a program's
    # credential and its owner's admin flag stays theirs to exercise by
    # signing in.
    minted = principal.api_key is not None and principal.api_key.minted_by is not None
    by_key = principal.api_key is not None and not minted

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
        billing_group=principal.billing_group.name,
    )
