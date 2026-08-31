"""The exchange and revocation half of ADR 0046.

The gateway is the issuer for its own local accounts: ``POST /auth/login``
(ADR 0043, with the ``client`` field of ADR 0046) mints a refresh credential,
this router trades it for short-lived ``/v1`` access keys and kills the family
on logout.

The decisive constraint is in ``deps.get_principal``: a credential is routed by
shape, and everything not JWT-shaped goes to ``resolve_api_key`` — so an access
credential *is* an ``ApiKey`` row, and revocation, disabled users, TTL and the
pinned query budget arrive with it. There is no new validation code on ``/v1``
on purpose; if a check seems to be missing here, it lives there.

One consequence shaped the exchange: the secret is stored **hashed only**, so
an exchange cannot "return the existing credential" — it mints a fresh one and
sweeps this client's expired and revoked rows. Live rows are left alone,
because a concurrent turn may already be holding one and revoking it would 401
a request that did nothing wrong. Growth is bounded by in-flight concurrency,
not by a cleanup job.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select

from gateway.deps import SessionDep
from gateway.errors import AuthenticationError
from gateway.models import ApiKey, RefreshCredential, User
from gateway.security import extract_prefix, generate_api_key, verify_api_key
from gateway.types import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["local-credentials"])

#: An access key is presented on every request and minted per exchange, so its
#: lifetime is the only bound on how long a leaked one works. Fifteen minutes
#: is comfortably longer than a streamed turn and short enough that the sweep
#: keeps the table at one row per concurrent exchange.
ACCESS_TTL_SECONDS = 900

#: The prefix distinguishes machine-minted credentials from person-made keys in
#: logs and listings at a glance. The lookup does not depend on it.
_ACCESS_PREFIX = "gwa"
_REFRESH_PREFIX = "gwr"

#: Both bodies carry a credential; a short field keeps a pasted JWT from being
#: accepted here by accident, since this endpoint is for our own shaped values.
_CREDENTIAL_FIELD = Field(min_length=12, max_length=512)


class TokenExchangeRequest(BaseModel):
    refresh_token: str = _CREDENTIAL_FIELD


class RevokeRequest(BaseModel):
    refresh_token: str = _CREDENTIAL_FIELD


def _reject(reason: str, prefix: str | None) -> AuthenticationError:
    # Logged with the prefix — an operator debugging a chat outage needs to see
    # *which* credential failed — but returned without it: the reason a
    # credential failed is a map for whoever is holding a forged one (ADR 0040's
    # rule, applied to the local family).
    logger.info("refresh credential rejected: prefix=%s reason=%s", prefix, reason)
    return AuthenticationError("The refresh credential is not valid.")


@router.post("/token")
async def exchange(body: TokenExchangeRequest, session: SessionDep) -> Any:
    """Trade a refresh credential for a short-lived access credential.

    Failure answers are uniform (unknown, malformed, expired, revoked, disabled
    user): the caller holds either a valid 2^256 secret or something that is
    not, and one message says all there is to say.
    """
    prefix = extract_prefix(body.refresh_token)
    row: RefreshCredential | None = None
    if prefix is not None:
        row = (
            await session.execute(
                select(RefreshCredential).where(RefreshCredential.prefix == prefix)
            )
        ).scalar_one_or_none()
    if row is None or not verify_api_key(body.refresh_token, row.secret_hash):
        raise _reject("no such credential", prefix)
    if row.expires_at <= utcnow():
        raise _reject("expired", prefix)

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise _reject("user inactive", prefix)

    # Sweep this client's dead rows, never the live ones — see the module
    # docstring for why revoking a live key here would break a concurrent turn.
    now = utcnow()
    await session.execute(
        delete(ApiKey).where(
            ApiKey.user_id == user.id,
            ApiKey.minted_by == row.client,
            or_(ApiKey.revoked_at.is_not(None), ApiKey.expires_at <= now),
        )
    )
    generated = generate_api_key(environment_prefix=_ACCESS_PREFIX)
    session.add(
        ApiKey(
            user_id=user.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            # Names the family in a DBA's query; the listing filter is on
            # `minted_by`, not on the name.
            name=f"local:{row.client}",
            minted_by=row.client,
            # Never pinned: a bearer caller cannot pin either — the user's
            # default billing group applies, with the same membership re-check
            # (resolve_billing_group).
            billing_group_id=None,
            expires_at=now + timedelta(seconds=ACCESS_TTL_SECONDS),
        )
    )
    row.last_used_at = now
    await session.commit()

    return {
        "access_token": generated.secret,
        "token_type": "bearer",
        "expires_in": ACCESS_TTL_SECONDS,
    }


@router.post("/revoke", status_code=204)
async def revoke(body: RevokeRequest, session: SessionDep) -> Response:
    """End the credential family: the refresh row and every access key it minted.

    This is what makes chat logout real — deleting the chat session row alone
    would leave a working `/v1` credential behind. Idempotent and uniform: an
    unknown or already-revoked credential answers 204 like a known one, because
    the difference has no value to anyone but its holder, who already knows.
    """
    prefix = extract_prefix(body.refresh_token)
    row: RefreshCredential | None = None
    if prefix is not None:
        row = (
            await session.execute(
                select(RefreshCredential).where(RefreshCredential.prefix == prefix)
            )
        ).scalar_one_or_none()
    if row is not None and verify_api_key(body.refresh_token, row.secret_hash):
        await session.execute(
            delete(ApiKey).where(
                ApiKey.user_id == row.user_id,
                ApiKey.minted_by == row.client,
            )
        )
        await session.delete(row)
        await session.commit()
        logger.info("refresh credential revoked: user=%s client=%s", row.user_id, row.client)
    return Response(status_code=204)
