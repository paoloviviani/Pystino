"""Rolling-window bucket arithmetic.

Pure maths with an explicit clock, so the awkward cases — window edges, bucket
rotation, very long windows — are testable without a datastore or a sleep.
"""

from __future__ import annotations

import pytest
from gateway.quota.windows import WindowSpec, choose_granularity


class TestChooseGranularity:
    def test_short_window_gets_one_second_buckets(self) -> None:
        assert choose_granularity(60, max_buckets_per_window=60) == 1

    def test_long_window_widens_buckets_to_stay_within_the_key_budget(self) -> None:
        day = 86_400
        granularity = choose_granularity(day, max_buckets_per_window=60)
        assert granularity == day // 60
        assert WindowSpec(day, granularity).bucket_count(now=1_000_000) <= 61

    def test_thirty_day_window_is_still_one_batched_read(self) -> None:
        month = 30 * 86_400
        spec = WindowSpec.for_window(month, max_buckets_per_window=60)
        assert spec.bucket_count(now=1_700_000_000) <= 61

    def test_never_narrower_than_the_configured_minimum(self) -> None:
        assert choose_granularity(600, min_bucket_seconds=30, max_buckets_per_window=600) == 30

    def test_never_wider_than_the_window_itself(self) -> None:
        assert choose_granularity(5, min_bucket_seconds=60) == 5

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_nonsense_windows(self, bad: int) -> None:
        with pytest.raises(ValueError):
            choose_granularity(bad)


class TestWindowSpec:
    def test_bucket_index_is_floor_division(self) -> None:
        spec = WindowSpec(window_seconds=60, granularity_seconds=10)
        assert spec.bucket_index(0) == 0
        assert spec.bucket_index(9.99) == 0
        assert spec.bucket_index(10) == 1
        assert spec.bucket_index(105) == 10

    def test_indices_cover_the_window_inclusively(self) -> None:
        """The bucket containing now-window overlaps the window and must count."""
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        assert spec.indices_for(now=100) == [7, 8, 9, 10]

    def test_indices_advance_as_time_passes(self) -> None:
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        before = spec.indices_for(now=100)
        after = spec.indices_for(now=110)
        assert after[-1] == before[-1] + 1
        assert after[0] == before[0] + 1

    def test_over_counting_is_bounded_by_one_bucket(self) -> None:
        """The documented approximation: at most one extra bucket of history.

        Erring toward over-counting refuses slightly early rather than letting
        spend escape, which is the correct bias for a quota.
        """
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        covered = len(spec.indices_for(now=100)) * spec.granularity_seconds
        assert spec.window_seconds < covered <= spec.window_seconds + spec.granularity_seconds

    def test_ttl_outlives_the_window(self) -> None:
        """A bucket must never expire while a reader can still need it."""
        spec = WindowSpec(window_seconds=60, granularity_seconds=10)
        assert spec.ttl_seconds() == 80
        assert spec.ttl_seconds() > spec.window_seconds


class TestRetryAfter:
    def test_uses_the_oldest_contributing_bucket(self) -> None:
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        # Traffic in bucket 8 only; bucket 8 ends at t=90, so it leaves the
        # window at 90 + 30 = 120, i.e. 20s after now=100.
        assert spec.retry_after_seconds(now=100, bucket_values={8: 5}) == 20

    def test_empty_window_still_suggests_a_positive_delay(self) -> None:
        """Retry-After: 0 invites an immediate retry storm."""
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        assert spec.retry_after_seconds(now=100, bucket_values={}) == 1
        assert spec.retry_after_seconds(now=100, bucket_values={8: 0}) == 1

    def test_ignores_empty_buckets_when_choosing_the_oldest(self) -> None:
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        # Bucket 7 is empty, so bucket 9 decides: it ends at t=100 and leaves the
        # window at 100 + 30 = 130, i.e. 30s after now.
        assert spec.retry_after_seconds(now=100, bucket_values={7: 0, 9: 3}) == 30
