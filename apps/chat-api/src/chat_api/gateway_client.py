"""Talking to the gateway.

Everything this service knows about the gateway is in this file, and it is
deliberately very little: a base URL, and the fact that ``/v1`` is
OpenAI-shaped. No shared types, no imported client — the boundary is HTTP, and
keeping the knowledge in one module is what makes that checkable.

The one rule that matters: **every call carries the access token of the person
whose request it is.** There is no service credential to fall back to, so a bug
that loses the token produces a 401 rather than a request nobody is billed for.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """The gateway refused or could not be reached."""

    def __init__(self, message: str, *, status: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    owned_by: str | None = None
    #: What the gateway says this model can do — "reasoning", "tools",
    #: "json_mode". Passed through rather than interpreted: which of them mean
    #: anything is the caller's business, and the list grows upstream.
    supported_features: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    """What a local sign-in returns, beyond the refresh credential.

    The gateway's local login (ADR 0043) answers with the identity a client
    session needs precisely so this service never has to parse the gateway's
    session cookie — which it cannot read anyway, scoped to the gateway's own
    paths. ``subject`` is computed here, not returned: it is the casefolded
    email, which is the key the gateway itself logs in by
    (``User.subject == email.casefold()``).
    """

    refresh_token: str
    email: str | None
    display_name: str | None
    groups: list[str]
    is_admin: bool

    @property
    def subject(self) -> str:
        assert self.email is not None
        return self.email.casefold()


class GatewayClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {access_token}"}

    # -- the local credential family (ADR 0046) ------------------------------
    #
    # These four are management-plane calls, not /v1: they ride the same HTTP
    # client and the same error discipline (the gateway's own words carried
    # through), but authenticate nobody here — they either name a client and
    # carry a password, or carry a credential the gateway judges on its own.

    async def auth_methods(self) -> dict[str, bool]:
        """Which ways in the gateway offers. Unauthenticated by its design."""
        try:
            response = await self._http.get(f"{self._base}/auth/methods")
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc
        if response.status_code != 200:
            raise self._error_from(response)
        payload = response.json()
        return {"local": bool(payload.get("local")), "oidc": bool(payload.get("oidc"))}

    async def local_login(self, *, email: str, password: str, client: str) -> LocalIdentity:
        """Sign in at the gateway's local door, asking for a machine credential.

        The password transits this service exactly once, in this POST body,
        over loopback or TLS — that is the accepted cost named in the ADR. The
        gateway owns the throttle and the Argon2 verification; a 401 here is
        its uniform "incorrect email or password", carried through verbatim.
        """
        try:
            response = await self._http.post(
                f"{self._base}/auth/login",
                json={"email": email, "password": password, "client": client},
            )
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc
        if response.status_code != 200:
            raise self._error_from(response)
        payload = response.json()
        refresh = payload.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise GatewayError(
                "the gateway accepted the sign-in but issued no client credential",
                status=502,
            )
        return LocalIdentity(
            refresh_token=refresh,
            email=payload.get("email") if isinstance(payload.get("email"), str) else None,
            display_name=(
                payload.get("display_name")
                if isinstance(payload.get("display_name"), str)
                else None
            ),
            groups=[g for g in payload.get("groups", []) if isinstance(g, str)],
            is_admin=bool(payload.get("is_admin")),
        )

    async def exchange_local(self, refresh_token: str) -> tuple[str, str]:
        """Trade the refresh credential for a short-lived access credential.

        Returns ``(access_token, refresh_token)`` with the second unchanged:
        rotation is deferred in the ADR, so the exchange is a mint, not a
        rotate, and the session row keeps what it has.
        """
        try:
            response = await self._http.post(
                f"{self._base}/auth/token", json={"refresh_token": refresh_token}
            )
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}", status=502) from exc
        if response.status_code != 200:
            raise self._error_from(response)
        payload = response.json()
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise GatewayError("the gateway issued no access credential", status=502)
        return access, refresh_token

    async def revoke_local(self, refresh_token: str) -> None:
        """End the credential family at the gateway. 204 whatever the answer.

        Logout must be real even when the gateway cannot be asked — the session
        row is deleted by the caller regardless — so a failure here is logged,
        not raised. The cost is named in the ADR: a credential the gateway
        could not be told about outlives this service's knowledge of it.
        """
        try:
            response = await self._http.post(
                f"{self._base}/auth/revoke", json={"refresh_token": refresh_token}
            )
        except httpx.HTTPError as exc:
            logger.warning("could not revoke the gateway credential: %s", exc)
            return
        if response.status_code != 204:
            logger.warning(
                "gateway answered %s to a revocation", response.status_code
            )

    async def models(self, access_token: str) -> list[Model]:
        """What this person may use — the gateway has already filtered it.

        No caching. The list is per-caller and changes when an administrator
        changes access; a cache here would show someone a model they cannot use
        and call it a gateway error when they tried.
        """
        try:
            response = await self._http.get(
                f"{self._base}/v1/models", headers=self._headers(access_token)
            )
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc
        if response.status_code != 200:
            raise self._error_from(response)
        payload = response.json()
        return [
            Model(
                id=item["id"],
                owned_by=item.get("owned_by"),
                supported_features=tuple(item.get("supported_features") or ()),
            )
            for item in payload.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    async def stream_chat(
        self, *, access_token: str, body: dict[str, Any], request_id: str
    ) -> AsyncIterator[str]:
        """Relay a streamed completion, yielding raw SSE lines.

        **The request id is ours, and is sent rather than read back.** The
        gateway adopts an inbound ``x-request-id`` and generates one otherwise,
        but never returns it — and could not usefully return it on a stream,
        where the headers are written before the row exists. So the caller
        supplies it, and the transcript and the ledger share an id by
        construction instead of by hoping for an echo. Found by
        ``scripts/test_chat_live.py``: every stored message had a null
        ``request_id``, which is exactly the column that makes spend
        explainable.

        Streamed rather than buffered even though buffering would be simpler,
        because the gateway's own comment about `flush_interval` applies one
        layer up: a buffered stream produces no error, and all the bytes are
        correct and all of them are late.

        `stream_options.include_usage` is requested so the terminal frame
        carries token counts. The gateway meters regardless — it does not trust
        us — but a client that never sees usage cannot show it.
        """
        payload = dict(body, stream=True)
        payload.setdefault("stream_options", {"include_usage": True})
        try:
            async with self._http.stream(
                "POST",
                f"{self._base}/v1/chat/completions",
                headers={**self._headers(access_token), "x-request-id": request_id},
                json=payload,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise self._error_from(response)
                async for line in response.aiter_lines():
                    yield line
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc

    @staticmethod
    def _error_from(response: httpx.Response) -> GatewayError:
        """Carry the gateway's own words through.

        Rewriting them is how "you have exceeded your monthly budget" becomes
        "something went wrong" on the screen the user actually reads.
        """
        message = f"gateway returned HTTP {response.status_code}"
        code = None
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or message)
                code = error.get("code")
        except ValueError:
            pass
        return GatewayError(message, status=response.status_code, code=code)
