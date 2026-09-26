"""User-defined quota notifications (ADR 0052).

The rule is the administrator's; the threshold is the user's. This module
holds the machinery that keeps the difference honest:

* **Zero queries on the request path.** Admission already runs per request,
  and its round trips are pinned by a test. The notifier polls the settings
  and the announcement memory on its own clock (10s, like every other
  config-poller here), so :meth:`QuotaNotifier.observe` — called from inside
  admission — is dictionary work and nothing more.
* **Crossing, not presence.** A threshold fires when the live percentage
  reaches it *above* the highest one already announced for that (user, rule),
  and the mark is pulled back down when usage falls below it again — a reset
  or the natural decay of a rolling window re-arms the threshold. Without the
  re-arm, one crossing in March would buy silence forever.
* **Off the request path.** A crossing spawns a detached task that reads the
  mail configuration, renders the message and sends it. A slow or broken mail
  server cannot delay admission; the announcement memory was already updated,
  so a delivery failure costs one email, not a flood of retries.
"""

import asyncio
import contextlib
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import Settings
from gateway.email_config import effective_smtp
from gateway.mail import send_mail_async
from gateway.models import LimitRule, QuotaNotificationSetting, QuotaNotificationState, User
from gateway.secrets import SecretBox

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 10.0


class QuotaNotifier:
    """Watches quota percentages against the thresholds users asked for."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        secrets: SecretBox,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        background_tasks: set[asyncio.Task[None]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._secrets = secrets
        self._refresh_seconds = refresh_seconds
        # Strong references to the detached sends, so a crossing's email task
        # cannot be garbage-collected mid-flight (main.py's pattern).
        self._background_tasks = background_tasks if background_tasks is not None else set()
        # rule_id -> [(user_id, threshold), ...]; the poller's view.
        self._subscriptions: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {}
        # (rule_id, user_id) -> highest pct already announced.
        self._announced: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def refresh_seconds(self) -> float:
        return self._refresh_seconds

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                await self.refresh_once()
            except Exception:
                logger.exception("quota notification poll failed; keeping current view")

    async def refresh_once(self) -> None:
        """Reload the subscriptions and the announcement memory."""
        async with self._session_factory() as session:
            settings_rows = (
                await session.execute(
                    select(
                        QuotaNotificationSetting.rule_id,
                        QuotaNotificationSetting.user_id,
                        QuotaNotificationSetting.threshold,
                    )
                )
            ).all()
            state_rows = (
                await session.execute(
                    select(
                        QuotaNotificationState.rule_id,
                        QuotaNotificationState.user_id,
                        QuotaNotificationState.notified_pct,
                    )
                )
            ).all()

        subscriptions: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {}
        for rule_id, user_id, threshold in settings_rows:
            subscriptions.setdefault(rule_id, []).append((user_id, threshold))
        self._subscriptions = subscriptions

        announced: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
        for rule_id, user_id, pct in state_rows:
            announced[(rule_id, user_id)] = pct
        # Keep live memory for keys that still have subscriptions; a state row
        # whose settings are gone is dead weight, and a settings row without
        # state starts from zero, which is what "never announced" means.
        self._announced = {
            key: pct for key, pct in announced.items() if key[0] in subscriptions
        }

    def observe(self, entries: list[tuple[LimitRule, int]]) -> None:
        """Admission's view of the rules it just measured.

        ``entries`` is (rule, usage percentage). Synchronous and in-memory by
        design: this runs inside every admitted request, and a query here
        would break the round-trip budget that test_query_counts.py pins.
        """
        if not self._subscriptions or not entries:
            return
        for rule, pct in entries:
            subscriptions = self._subscriptions.get(rule.id)
            if not subscriptions:
                continue
            for user_id, threshold in subscriptions:
                key = (rule.id, user_id)
                announced = self._announced.get(key, 0)
                if pct < announced:
                    # Usage fell back under the announced mark: re-arm.
                    self._announced[key] = pct
                    continue
                if pct >= threshold and announced < threshold:
                    # Crossed upward past this user's line. The memory updates
                    # now, synchronously: a crash after this point must not
                    # turn one crossing into a mail flood.
                    self._announced[key] = min(pct, 100)
                    self._schedule_send(rule, user_id, threshold, pct)

    def _schedule_send(
        self, rule: LimitRule, user_id: uuid.UUID, threshold: int, pct: int
    ) -> None:
        task = asyncio.create_task(self._send(rule, user_id, threshold, pct))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _send(self, rule: LimitRule, user_id: uuid.UUID, threshold: int, pct: int) -> None:
        try:
            async with self._session_factory() as session:
                email = await session.scalar(
                    select(User.email).where(User.id == user_id)
                )
                if not email:
                    # A user with no email address cannot be mailed; their
                    # thresholds stay configured and simply do nothing.
                    return
                effective = await effective_smtp(session, self._settings, self._secrets)

            if not effective.usable:
                # The memory already recorded the crossing; when the mail
                # configuration arrives, the next crossing tells them.
                logger.info("quota notification skipped: email is not configured")
                return

            await send_mail_async(
                effective.to_smtp_settings(),
                email,
                f"Quota notice — {rule.name} at {pct}%",
                f'Your quota "{rule.name}" has reached {pct}% of its limit '
                f"(you asked to be told at {threshold}%).\n\n"
                f"The window is {rule.window_label}; the console's Overview "
                "shows the exact figures.\n\n"
                "You can change or remove this notice in the console, on the "
                "Overview screen, under Quotas.",
            )
        except Exception:
            # The crossing was already recorded; a failed send must not retry
            # its way into a flood, and must not take down its parent task.
            logger.exception("quota notification mail failed (rule %s)", rule.id)

# -- user API ------------------------------------------------------------------


async def thresholds_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[uuid.UUID, list[int]]:
    rows = (
        await session.execute(
            select(
                QuotaNotificationSetting.rule_id, QuotaNotificationSetting.threshold
            ).where(QuotaNotificationSetting.user_id == user_id)
        )
    ).all()
    thresholds: dict[uuid.UUID, list[int]] = {}
    for rule_id, threshold in rows:
        thresholds.setdefault(rule_id, []).append(threshold)
    return {rule_id: sorted(values) for rule_id, values in thresholds.items()}


async def replace_thresholds(
    session: AsyncSession, user_id: uuid.UUID, rule_id: uuid.UUID, thresholds: list[int]
) -> None:
    cleaned = sorted({int(value) for value in thresholds if 1 <= int(value) <= 100})
    await session.execute(
        delete(QuotaNotificationSetting).where(
            QuotaNotificationSetting.user_id == user_id,
            QuotaNotificationSetting.rule_id == rule_id,
        )
    )
    for threshold in cleaned:
        session.add(
            QuotaNotificationSetting(
                user_id=user_id, rule_id=rule_id, threshold=threshold
            )
        )
    # A fresh decision clears the memory: if the usage is already past the new
    # threshold, the next poll announces it — which is what the user asked for.
    await session.execute(
        delete(QuotaNotificationState).where(
            QuotaNotificationState.user_id == user_id,
            QuotaNotificationState.rule_id == rule_id,
        )
    )
    await session.commit()
