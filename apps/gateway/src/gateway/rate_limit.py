"""A tiny in-process rate limiter (ADR 0093 §4.1).

Written for one caller: ``POST /v1/session/announce``, 10 per minute per
``(issuer, subject)``. The codebase's one existing rate-limiting facility is
the quota engine (``quota/engine.py``), which meters spend against a billing
group; announce runs before a billing group is even resolved; and it is not
priced. Building this on the quota engine would mean giving a sign-in call a
billing identity it does not have, for a ceiling that has nothing to do with
spend. Nothing else in the tree is a general-purpose request limiter, so
this is a small one, in process memory, good enough for the ceiling it
enforces.

Not shared across workers: the gateway image runs two by default
(``Dockerfile``), so the effective ceiling for a given identity is up to
2x what is configured here. Worth knowing, not worth a shared store — a
per-identity ceiling this generous is about catching a runaway client, not
bounding capacity precisely.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, *, max_calls: int, window_seconds: float) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, key: tuple[str, str]) -> bool:
        """True and records a hit, or False with nothing recorded.

        Synchronous and non-blocking on purpose: called from inside an
        `async def` route, but nothing here ever awaits, so there is no
        window in which two concurrent requests on the same event loop could
        both read the same, stale count.
        """
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self._window_seconds:
            hits.popleft()
        if len(hits) >= self._max_calls:
            return False
        hits.append(now)
        return True
