"""Chat erasure (ADR 0093 §9.2, §9.3): delete a person from the chat too, an
idempotent, retried, audited call over the compose network with its own
service credential.

The first attempt happens inline, right after the gateway's own delete
transaction commits (`attempt_erasure`, called directly). Everything after
that first failure is `ErasureRetryLoop`, a background poller in the same
shape as `fx.FXService`: it claims one due row at a time with `FOR UPDATE
SKIP LOCKED` so two gateway workers polling at once never send the same
`erasure_id` twice concurrently, backs it off exponentially on failure, and
never gives up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import Settings
from gateway.identity_events import record_event
from gateway.models import ChatErasure, ChatErasureStatus, IdentityEventAction, IdentityEventActor
from gateway.types import utcnow

logger = logging.getLogger(__name__)

#: §9.3: "exponential backoff from 30 s up to 1 h".
_BACKOFF_FLOOR_SECONDS = 30
_BACKOFF_CEILING_SECONDS = 3600
#: §9.3: "the first failure and every tenth after it".
_AUDIT_EVERY = 10
_DEFAULT_POLL_SECONDS = 30.0


def backoff_seconds(attempts: int) -> int:
    """`attempts` is the count *after* the failure this schedules the next
    try for: 1 -> 30s, 2 -> 60s, 3 -> 120s, ... capped at 3600s."""
    doubled = _BACKOFF_FLOOR_SECONDS * (2 ** max(attempts - 1, 0))
    return min(int(doubled), _BACKOFF_CEILING_SECONDS)


async def preview_chat_erasure(
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    gateway_user_id: uuid.UUID,
    identities: list[dict[str, str]],
) -> dict[str, Any] | None:
    """The chat's own dry-run counts for the delete-preview screen, or
    `None` when it could not be reached -- the preview says "chat counts
    unavailable" for that rather than blocking on it (§9.2). Never mutates
    anything on the chat's side."""
    if not settings.chat.erasure_url or not settings.chat.erasure_token.get_secret_value():
        return None
    url = settings.chat.erasure_url.rstrip("/") + "/preview"
    try:
        response = await http.post(
            url,
            json={"gateway_user_id": str(gateway_user_id), "identities": identities},
            headers=_auth_header(settings),
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("chat erasure preview unreachable: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


def _auth_header(settings: Settings) -> dict[str, str]:
    return {"authorization": f"Bearer {settings.chat.erasure_token.get_secret_value()}"}


async def queue_erasure(
    session: AsyncSession, *, gateway_user_id: uuid.UUID, identities: list[dict[str, str]]
) -> ChatErasure:
    """Inserts the `pending` row, as one step of the delete transaction
    (§9.3): part of the caller's own transaction, and does not commit."""
    row = ChatErasure(gateway_user_id=gateway_user_id, identities=identities)
    session.add(row)
    await session.flush()
    return row


async def attempt_erasure(
    session: AsyncSession, settings: Settings, http: httpx.AsyncClient, erasure: ChatErasure
) -> bool:
    """One try, with all of its gateway-side bookkeeping. Commits before
    returning either way. `erasure` must already be locked by the caller if
    it was read for that purpose (the retry loop does; the inline post-commit
    call does not need to, since nothing else can see the row yet)."""
    if erasure.status is ChatErasureStatus.DONE:
        return True
    if not settings.chat.erasure_url or not settings.chat.erasure_token.get_secret_value():
        # Reachable only if the setting was cleared after rows were already
        # queued -- an operator-caused state, not a caller's mistake. Treat
        # it exactly like any other failure to reach the chat: back off and
        # keep retrying, since the setting may come back.
        return await _record_failure(
            session, erasure, "GATEWAY_CHAT__ERASURE_URL or __ERASURE_TOKEN is not set"
        )

    try:
        response = await http.post(
            settings.chat.erasure_url,
            json={
                "erasure_id": str(erasure.id),
                "gateway_user_id": str(erasure.gateway_user_id),
                "identities": erasure.identities,
            },
            headers=_auth_header(settings),
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPError as exc:
        return await _record_failure(session, erasure, str(exc))

    counts = result.get("counts") if isinstance(result, dict) else None
    erasure.status = ChatErasureStatus.DONE
    erasure.done_at = utcnow()
    erasure.chat_counts = counts if isinstance(counts, dict) else {}
    erasure.last_error = None
    # §9.3: "identities cleared (no PII left beyond the audit labels)".
    erasure.identities = []
    await record_event(
        session,
        actor_type=IdentityEventActor.SYSTEM,
        actor_label="system",
        action=IdentityEventAction.CHAT_ERASURE_DONE,
        target_user_id=erasure.gateway_user_id,
        detail={"counts": erasure.chat_counts},
    )
    await session.commit()
    return True


async def _record_failure(session: AsyncSession, erasure: ChatErasure, message: str) -> bool:
    erasure.attempts += 1
    erasure.last_error = message[:2000]
    erasure.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds(erasure.attempts))
    if erasure.attempts == 1 or erasure.attempts % _AUDIT_EVERY == 0:
        await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.CHAT_ERASURE_RETRYING,
            target_user_id=erasure.gateway_user_id,
            detail={"attempts": erasure.attempts},
        )
    await session.commit()
    return False


async def run_pending_erasures(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, http: httpx.AsyncClient
) -> int:
    """One pass over every due row, oldest first, one at a time. Each is
    claimed with `FOR UPDATE SKIP LOCKED`, so calling this concurrently from
    two workers is exactly the point, not just tolerated: neither ever waits
    on the other, and neither ever sends the same `erasure_id` twice at
    once. Returns how many rows were attempted, for the caller's own log.
    """
    attempted = 0
    while True:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ChatErasure)
                    .where(
                        ChatErasure.status == ChatErasureStatus.PENDING,
                        or_(
                            ChatErasure.next_attempt_at.is_(None),
                            ChatErasure.next_attempt_at <= utcnow(),
                        ),
                    )
                    .order_by(ChatErasure.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return attempted
            await attempt_erasure(session, settings, http, row)
            attempted += 1


class ErasureRetryLoop:
    """The background half (§9.3): started in the app lifespan, stopped at
    shutdown, the same shape as `fx.FXService`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._own_http = http is None
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._own_http:
            with contextlib.suppress(Exception):
                await self._http.aclose()

    async def _run(self) -> None:
        while True:
            try:
                await run_pending_erasures(self._session_factory, self._settings, self._http)
            except Exception:
                logger.exception("chat erasure retry pass failed")
            await asyncio.sleep(self._poll_seconds)
