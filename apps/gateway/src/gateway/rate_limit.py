"""A tiny in-process rate limiter (ADR 0093 §4.1).

Written for one caller: ``POST /v1/session/announce``, **10 per minute per
identity per worker process** — not a strict, deployment-wide ceiling, and
deliberately so. Announce already needs a valid, signature-checked chat-client
token to reach this point; the limiter damps a looping or buggy client, it is
not a security boundary, so a shared store precise enough to make the ceiling
exact (the gateway image runs two workers by default, ``Dockerfile``, so the
same identity's real ceiling is up to 2x this) is not worth its cost here.

The codebase's one existing rate-limiting facility is the quota engine
(``quota/engine.py``), which meters spend against a billing group; announce
runs before a billing group is even resolved, and is not priced. Building
this on the quota engine would mean giving a sign-in call a billing identity
it does not have, for a ceiling that has nothing to do with spend. Nothing
else in the tree is a general-purpose request limiter, so this is a small
one, in process memory, good enough for what it is damping.
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
