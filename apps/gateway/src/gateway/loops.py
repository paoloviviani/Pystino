"""Shared stop for the background loops that begin with work, not a sleep."""

from __future__ import annotations

import asyncio
import contextlib

#: How long a stop waits for a pass already running before cancelling it.
STOP_GRACE_SECONDS = 10.0


async def stop_between_passes(
    task: asyncio.Task[None], stopping: asyncio.Event, *, grace: float = STOP_GRACE_SECONDS
) -> None:
    """Ask a loop to stop after its current pass, and wait for it.

    A loop whose first pass starts the moment it is created is easily stopped
    while asyncpg is still connecting or mid-query. Cancelling it there strands
    that connection: the pool never gets it back, so the engine's dispose does
    not close it and its socket is reported unclosed at garbage collection.
    The loop checks `stopping` between passes; only a pass that overruns the
    grace period is cancelled.
    """
    stopping.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=grace)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
