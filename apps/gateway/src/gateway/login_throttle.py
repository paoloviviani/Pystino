"""Brute-force throttle for local login: failed attempts per email, in process.

Deliberately **not** in Valkey. The counter store is a rebuildable cache for
quotas — losing it loses nothing that matters — but a login throttle that
vanishes when the cache is flushed is no throttle at all. Yet it cannot live
in PostgreSQL either: a database round trip per login attempt is fine, but the
gateway must fail *closed* if the counter is unreachable, and the quota path's
"fall back to the database" pattern is exactly the fail-open shape a
brute-force defence may not take.

In-process gives fail-closed for free (the counter cannot be lost without the
process dying), at one known cost: with several workers the limit is
``max_failed_attempts`` per worker, not in total. That weakens the throttle by
exactly a factor of the worker count — an attacker gets, say, 30 tries instead
of 10 before the window closes — while Argon2id keeps each try expensive. A
correct shared throttle needs either sticky sessions or a store whose loss is
tolerable, and this deployment has neither; the per-worker arithmetic is a
known quantity rather than a silent gap, and it is written here so the next
reader does not have to rediscover it.

Memory: entries are pruned on access, and one entry is two floats and a
counter per *attempted* email. Unbounded growth would need an attacker willing
to script millions of distinct addresses through a login form, which the
attempt limit itself makes pointless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class _Attempt:
    failures: int = 0
    window_started: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class LoginThrottle:
    """Counts failed logins per email; ``allowed`` decides, ``record`` reports."""

    max_failed_attempts: int
    window_seconds: float
    _attempts: dict[str, _Attempt] = field(default_factory=dict)

    def allowed(self, email: str) -> bool:
        """May this address try again now?

        Unknown addresses are always allowed — the *existence* of an account is
        not something this class knows, and the login route answers unknown
        addresses with the same dummy-verification cost anyway.
        """
        attempt = self._attempts.get(email.casefold())
        if attempt is None:
            return True
        if time.monotonic() - attempt.window_started >= self.window_seconds:
            # The window closed: forget rather than count on. A throttled
            # address starts its next window from zero, not from a residue.
            del self._attempts[email.casefold()]
            return True
        return attempt.failures < self.max_failed_attempts

    def record_failure(self, email: str) -> None:
        attempt = self._attempts.setdefault(email.casefold(), _Attempt())
        if time.monotonic() - attempt.window_started >= self.window_seconds:
            attempt.failures = 0
            attempt.window_started = time.monotonic()
        attempt.failures += 1

    def record_success(self, email: str) -> None:
        self._attempts.pop(email.casefold(), None)
