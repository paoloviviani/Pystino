"""Running a provider's sync: on demand, on a schedule, never twice at once."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import Settings
from gateway.directory.adapters import AdapterError, build_adapter
from gateway.directory.engine import SyncReport, apply_entries, record_run
from gateway.identity_registry import record_from_row
from gateway.models import DirectorySyncRun, IdentityProvider
from gateway.secrets import SecretBox
from gateway.types import utcnow

logger = logging.getLogger(__name__)

PULL_ADAPTERS = ("authelia_file", "keycloak_admin")
LOCK_SECONDS = 600


def decrypt_config(row: IdentityProvider, secrets: SecretBox) -> str | None:
    return secrets.decrypt(row.sync_config_encrypted) if row.sync_config_encrypted else None


async def run_pull(
    session_factory: async_sessionmaker[AsyncSession],
    secrets: SecretBox,
    http: Any,
    settings: Settings,
    provider_id: uuid.UUID,
    *,
    trigger: str,
    dry_run: bool,
    force: bool = False,
    started_by: uuid.UUID | None = None,
) -> tuple[SyncReport, DirectorySyncRun]:
    """List the directory and apply it. The first run of an adapter is a dry run.

    "First run is a dry run" is enforced here, not in the console: until an
    administrator has looked at what the adapter would do and confirmed it,
    no scheduled or manual run may change an account.
    """
    started = utcnow()
    async with session_factory() as session:
        row = await session.get(IdentityProvider, provider_id)
        if row is None:
            raise LookupError(f"no identity provider {provider_id}")
        record = record_from_row(row, secrets)
        config = decrypt_config(row, secrets)
    if record.sync_adapter not in PULL_ADAPTERS:
        raise AdapterError(f"{record.sync_adapter!r} is not a pull adapter")
    if not record.sync_confirmed:
        dry_run = True
    try:
        entries = await build_adapter(record, config, http).list_entries()
    except (AdapterError, OSError, ValueError, KeyError) as exc:
        report = SyncReport(status="failed", dry_run=dry_run, error=str(exc))
    else:
        async with session_factory() as session:
            try:
                report = await apply_entries(
                    session,
                    record,
                    entries,
                    settings=settings.oidc,
                    dry_run=dry_run,
                    full=True,
                    force=force,
                )
            except Exception as exc:  # recorded, not raised: a run's outcome is data
                await session.rollback()
                logger.exception("directory sync for %s failed", record.name)
                report = SyncReport(status="failed", dry_run=dry_run, error=str(exc))
    async with session_factory() as session:
        run = await record_run(
            session, provider_id, report, trigger=trigger, started_at=started, started_by=started_by
        )
    return report, run


class DirectoryScheduler:
    """Runs due pulls. A Valkey lock per provider keeps two workers from racing."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secrets: SecretBox,
        http: Any,
        settings: Settings,
        valkey: Any | None,
        *,
        tick_seconds: float = 60.0,
    ) -> None:
        self._session_factory = session_factory
        self._secrets = secrets
        self._http = http
        self._settings = settings
        self._valkey = valkey
        self._tick = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._local_locks: set[uuid.UUID] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            try:
                await self.run_due()
            except Exception:
                logger.warning("directory scheduler tick failed", exc_info=True)

    async def _acquire(self, provider_id: uuid.UUID) -> bool:
        if self._valkey is not None:
            return bool(
                await self._valkey.set(
                    f"pystino:dirsync:{provider_id}", "1", nx=True, ex=LOCK_SECONDS
                )
            )
        if provider_id in self._local_locks:
            return False
        self._local_locks.add(provider_id)
        return True

    async def _release(self, provider_id: uuid.UUID) -> None:
        if self._valkey is not None:
            await self._valkey.delete(f"pystino:dirsync:{provider_id}")
        self._local_locks.discard(provider_id)

    async def run_due(self) -> list[uuid.UUID]:
        now = utcnow()
        due: list[uuid.UUID] = []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(IdentityProvider).where(
                            IdentityProvider.is_enabled.is_(True),
                            IdentityProvider.sync_adapter.in_(PULL_ADAPTERS),
                            IdentityProvider.sync_confirmed.is_(True),
                            IdentityProvider.sync_interval_minutes > 0,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                last = (
                    await session.execute(
                        select(DirectorySyncRun.started_at)
                        .where(DirectorySyncRun.provider_id == row.id)
                        .order_by(DirectorySyncRun.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)  # SQLite hands back naive values
                if last is None or now - last >= timedelta(minutes=row.sync_interval_minutes):
                    due.append(row.id)
        ran = []
        for provider_id in due:
            if not await self._acquire(provider_id):
                continue
            try:
                await run_pull(
                    self._session_factory,
                    self._secrets,
                    self._http,
                    self._settings,
                    provider_id,
                    trigger="schedule",
                    dry_run=False,
                )
                ran.append(provider_id)
            finally:
                await self._release(provider_id)
        return ran
