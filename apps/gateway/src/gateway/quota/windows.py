"""Rolling-window bucket arithmetic.

Pure functions, no I/O, injectable clock — so the awkward cases (window edges,
bucket rotation, long windows) are unit-testable without a database or a Valkey.

A true rolling window would need every event's timestamp, which is O(events in
window) to sum on each request. Instead the window is approximated by fixed-width
buckets and the buckets overlapping the window are summed.

The approximation has one bounded error, and its direction is deliberate. The
oldest bucket in the window is included whole even though the window only covers
part of it, so a total is **over**-counted by at most the traffic in one bucket
width. Over-counting refuses slightly early; under-counting would let spend
escape. For a quota system, erring toward refusal is the correct bias, and it is
bounded by ``window_seconds / max_buckets_per_window``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def choose_granularity(
    window_seconds: int,
    *,
    min_bucket_seconds: int = 1,
    max_buckets_per_window: int = 60,
) -> int:
    """Pick a bucket width for *window_seconds*.

    Wide enough that a window never needs more than ``max_buckets_per_window``
    keys (so a total is one batched read), and never narrower than
    ``min_bucket_seconds``.

    A 60s window with 60 buckets gives 1s precision; a 30-day window gives 12h
    buckets, so a monthly budget is accurate to within half a day of traffic.
    Tighten ``max_buckets_per_window`` to trade read size for precision.
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if min_bucket_seconds <= 0:
        raise ValueError("min_bucket_seconds must be positive")
    if max_buckets_per_window <= 0:
        raise ValueError("max_buckets_per_window must be positive")

    granularity = max(min_bucket_seconds, math.ceil(window_seconds / max_buckets_per_window))
    # Never wider than the window itself, or a single bucket would represent more
    # history than the rule asks about.
    return min(granularity, window_seconds)


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """A window and the bucket grid used to approximate it."""

    window_seconds: int
    granularity_seconds: int

    @classmethod
    def for_window(
        cls,
        window_seconds: int,
        *,
        min_bucket_seconds: int = 1,
        max_buckets_per_window: int = 60,
    ) -> WindowSpec:
        return cls(
            window_seconds=window_seconds,
            granularity_seconds=choose_granularity(
                window_seconds,
                min_bucket_seconds=min_bucket_seconds,
                max_buckets_per_window=max_buckets_per_window,
            ),
        )

    def bucket_index(self, timestamp: float) -> int:
        """Index of the bucket containing *timestamp* (a unix time)."""
        return int(timestamp // self.granularity_seconds)

    def bucket_start(self, index: int) -> float:
        """Unix time at which bucket *index* begins."""
        return index * self.granularity_seconds

    def bucket_end(self, index: int) -> float:
        """Unix time at which bucket *index* ends (exclusive)."""
        return (index + 1) * self.granularity_seconds

    def indices_for(self, now: float) -> list[int]:
        """Every bucket overlapping the window ending at *now*.

        Inclusive at both ends: the bucket containing ``now - window_seconds``
        overlaps the window and so must be counted.
        """
        newest = self.bucket_index(now)
        oldest = self.bucket_index(now - self.window_seconds)
        return list(range(oldest, newest + 1))

    def bucket_count(self, now: float) -> int:
        return len(self.indices_for(now))

    def ttl_seconds(self) -> int:
        """How long a bucket must survive before it can be discarded.

        One window plus two bucket widths of slack, so a bucket is never expired
        while it can still be read: the window edge lands mid-bucket, and settle
        writes can arrive after a stream that outlived its own bucket.
        """
        return self.window_seconds + 2 * self.granularity_seconds

    def retry_after_seconds(self, now: float, bucket_values: dict[int, int]) -> int:
        """Seconds until the window could plausibly drop below its limit.

        The oldest bucket carrying traffic leaves the window
        ``window_seconds`` after that bucket ends, so that is the first moment
        the total is guaranteed to have fallen. An estimate, not a promise:
        traffic arriving in the meantime pushes it out again. Always at least 1,
        because ``Retry-After: 0`` invites an immediate retry storm.
        """
        contributing = [index for index, value in bucket_values.items() if value > 0]
        if not contributing:
            return 1
        oldest = min(contributing)
        expires_at = self.bucket_end(oldest) + self.window_seconds
        return max(1, math.ceil(expires_at - now))
