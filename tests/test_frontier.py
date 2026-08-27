"""Frontier tests.

The lease is the mechanism the whole politeness guarantee rests on: if two
workers can ever hold the same domain at once, per-host pacing is meaningless
because each worker paces only against its own clock. These tests target that
invariant specifically, plus the scheduling behaviour around it.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from scrapev3.frontier import (
    NUM_SHARDS,
    SQLiteFrontier,
    range_for_worker,
    shard_for,
    shard_ranges,
)
from scrapev3.frontier.models import to_ts, utcnow


@pytest.fixture
def store(tmp_path):
    f = SQLiteFrontier(tmp_path / "frontier.sqlite")
    f.create_schema()
    yield f
    f.close()


def seed(store, n=10):
    rows = [(1000 + i, f"https://site{i}.com/news", f"site{i}.com") for i in range(n)]
    store.upsert_sites(rows)
    return rows


class TestSharding:
    def test_stable_across_calls(self):
        assert shard_for("example.com") == shard_for("example.com")

    def test_in_range(self):
        for d in ("a.com", "bbc.co.uk", "house.gov", "mit.edu", ""):
            assert 0 <= shard_for(d) < NUM_SHARDS

    def test_not_pythons_randomized_hash(self):
        """Built-in hash() is salted per process; ours must not be.

        Hardcoding the expected value is the point: if this changes, every
        domain re-shards and workers silently trade ownership.
        """
        assert shard_for("house.gov") == 112

    def test_ranges_cover_every_shard_exactly_once(self):
        for workers in (1, 2, 3, 6, 7, 16, 64):
            ranges = shard_ranges(workers)
            assert len(ranges) == workers
            covered = [s for lo, hi in ranges for s in range(lo, hi + 1)]
            assert sorted(covered) == list(range(NUM_SHARDS))

    def test_range_sizes_differ_by_at_most_one(self):
        sizes = [hi - lo + 1 for lo, hi in shard_ranges(7)]
        assert max(sizes) - min(sizes) <= 1

    def test_rejects_bad_worker_counts(self):
        with pytest.raises(ValueError):
            shard_ranges(0)
        with pytest.raises(ValueError):
            range_for_worker(3, 3)


class TestLeaseExclusivity:
    def test_second_worker_cannot_take_leased_domains(self, store):
        seed(store, 5)
        first = store.acquire("w1", limit=5)
        assert len(first) == 5
        assert store.acquire("w2", limit=5) == []

    def test_concurrent_acquire_never_double_leases(self, store):
        """The invariant, under real threads."""
        seed(store, 60)
        claimed: list[str] = []
        lock = threading.Lock()

        def worker(name):
            f = SQLiteFrontier(store.path)
            try:
                got = f.acquire(name, limit=20)
                with lock:
                    claimed.extend(r.domain for r in got)
            finally:
                f.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(claimed) == len(set(claimed)), "a domain was leased twice"

    def test_shard_range_partitions_work(self, store):
        seed(store, 40)
        lo1, hi1 = range_for_worker(0, 2)
        lo2, hi2 = range_for_worker(1, 2)
        a = store.acquire("w1", shard_lo=lo1, shard_hi=hi1, limit=100)
        b = store.acquire("w2", shard_lo=lo2, shard_hi=hi2, limit=100)
        assert not ({r.domain for r in a} & {r.domain for r in b})
        assert len(a) + len(b) == 40

    def test_limit_is_respected(self, store):
        seed(store, 30)
        assert len(store.acquire("w1", limit=7)) == 7


class TestLeaseExpiry:
    def test_expired_lease_is_reclaimable(self, store):
        seed(store, 3)
        store.acquire("dead-worker", limit=3, lease_seconds=-1)  # already expired
        assert store.release_expired_leases() == 3
        assert len(store.acquire("w2", limit=3)) == 3

    def test_live_lease_is_not_reclaimed(self, store):
        seed(store, 3)
        store.acquire("w1", limit=3, lease_seconds=600)
        assert store.release_expired_leases() == 0
        assert store.acquire("w2", limit=3) == []


class TestScheduling:
    def test_success_schedules_one_revisit_period_out(self, store):
        seed(store, 1)
        store.acquire("w1", limit=1)
        store.release("site0.com", success=True)
        rec = store.get("site0.com")
        expected = utcnow() + timedelta(seconds=rec.revisit_period_s)
        assert abs((rec.next_allowed_at - expected).total_seconds()) < 5
        assert rec.consec_failures == 0
        assert rec.last_success_at is not None

    def test_failures_back_off_exponentially(self, store):
        seed(store, 1)
        seen = []
        for _ in range(4):
            store.release("site0.com", success=False)
            seen.append(store.get("site0.com").backoff_seconds())
        assert seen == [172800, 345600, 691200, 1382400]

    def test_backoff_is_capped(self, store):
        seed(store, 1)
        for _ in range(20):
            store.release("site0.com", success=False)
        rec = store.get("site0.com")
        assert rec.consec_failures == 20
        # Capped at 2**6 so a dead domain retries roughly every 64 days, not never.
        assert rec.backoff_seconds() == 86_400 * 64

    def test_success_clears_failure_streak(self, store):
        seed(store, 1)
        store.release("site0.com", success=False)
        store.release("site0.com", success=False)
        assert store.get("site0.com").consec_failures == 2
        store.release("site0.com", success=True)
        assert store.get("site0.com").consec_failures == 0

    def test_release_persists_discovery_state(self, store):
        seed(store, 1)
        store.release(
            "site0.com", success=True,
            discovery_method="rss", feed_url="https://site0.com/feed",
            etag='W/"abc"', needs_browser=True, p50_body_len=3200,
        )
        rec = store.get("site0.com")
        assert rec.discovery_method == "rss"
        assert rec.feed_url == "https://site0.com/feed"
        assert rec.etag == 'W/"abc"'
        assert rec.needs_browser is True
        assert rec.p50_body_len == 3200

    def test_release_of_unknown_domain_is_a_noop(self, store):
        store.release("nope.com", success=True)  # must not raise


class TestSeeding:
    def test_reseed_does_not_reset_schedule_or_failures(self, store):
        """Re-importing the site list must not wipe crawl state."""
        seed(store, 1)
        store.release("site0.com", success=False)
        before = store.get("site0.com")

        store.upsert_sites([(1000, "https://site0.com/newsroom", "site0.com")])
        after = store.get("site0.com")

        assert after.newsroom_url == "https://site0.com/newsroom"  # url refreshed
        assert after.consec_failures == before.consec_failures     # state kept
        assert after.next_allowed_at == before.next_allowed_at

    def test_disabled_domains_are_not_leased(self, store):
        seed(store, 3)
        store.disable("site1.com")
        got = store.acquire("w1", limit=10)
        assert "site1.com" not in {r.domain for r in got}

    def test_future_domains_are_not_due(self, store):
        seed(store, 2)
        store._execute(
            "UPDATE domain_state SET next_allowed_at = ? WHERE domain = ?",
            (to_ts(utcnow() + timedelta(days=1)), "site0.com"),
        )
        got = store.acquire("w1", limit=10)
        assert {r.domain for r in got} == {"site1.com"}

    def test_stats(self, store):
        seed(store, 5)
        store.disable("site0.com")
        store.release("site1.com", success=False)
        s = store.stats()
        assert s.total == 5
        assert s.enabled == 4
        assert s.failing == 1
        assert s.never_crawled == 5


class TestTargets:
    """Many newsroom URLs can share one domain.

    Caught by running against the real list: house.gov alone has 417 legislator
    press pages. Keying the frontier only on domain silently dropped 654 of the
    2,401 targets. Leasing stays per domain (politeness); crawling is per target.
    """

    def test_multiple_targets_on_one_domain_all_survive(self, store):
        store.upsert_sites([
            (1, "https://a.house.gov/press", "house.gov"),
            (2, "https://b.house.gov/press", "house.gov"),
            (3, "https://c.house.gov/news", "house.gov"),
        ])
        assert len(store.targets_for("house.gov")) == 3
        # ...but only one lease unit, because they are one origin.
        assert store.stats().total == 1
        assert store.stats().targets == 3

    def test_lease_returns_every_target_on_the_domain(self, store):
        store.upsert_sites([
            (1, "https://a.house.gov/press", "house.gov"),
            (2, "https://b.house.gov/press", "house.gov"),
        ])
        leased = store.acquire("w1", limit=10)
        assert len(leased) == 1
        assert len(leased[0].targets) == 2

    def test_one_lease_covers_the_shared_origin(self, store):
        """417 sibling pages must not be leasable by 417 workers."""
        store.upsert_sites([
            (i, f"https://rep{i}.house.gov/press", "house.gov") for i in range(20)
        ])
        assert len(store.acquire("w1", limit=100)) == 1
        assert store.acquire("w2", limit=100) == []

    def test_target_failure_is_tracked_separately_from_domain(self, store):
        store.upsert_sites([
            (1, "https://a.house.gov/press", "house.gov"),
            (2, "https://b.house.gov/press", "house.gov"),
        ])
        store.release_target("https://a.house.gov/press", success=False)
        by_url = {t.newsroom_url: t for t in store.targets_for("house.gov")}
        assert by_url["https://a.house.gov/press"].consec_failures == 1
        # One dead legislator page must not make house.gov look dead.
        assert by_url["https://b.house.gov/press"].consec_failures == 0
        assert store.get("house.gov").consec_failures == 0

    def test_target_success_records_discovery_state(self, store):
        store.upsert_sites([(1, "https://a.house.gov/press", "house.gov")])
        store.release_target(
            "https://a.house.gov/press", success=True,
            discovery_method="rss", feed_url="https://a.house.gov/rss",
        )
        t = store.targets_for("house.gov")[0]
        assert t.discovery_method == "rss"
        assert t.feed_url == "https://a.house.gov/rss"
        assert t.last_success_at is not None
        assert t.consec_failures == 0

    def test_reseed_is_idempotent_for_targets(self, store):
        rows = [
            (1, "https://a.house.gov/press", "house.gov"),
            (2, "https://b.house.gov/press", "house.gov"),
        ]
        store.upsert_sites(rows)
        store.upsert_sites(rows)
        assert len(store.targets_for("house.gov")) == 2


class TestTargetedCrawl:
    """Asking for one site by name.

    The scheduled path orders by `next_allowed_at`, so a domain re-crawled
    today sorts behind every domain never crawled at all - 1,747 of them in
    this corpus. Without a targeted lease, "re-run this one site" silently
    crawls somebody else's.
    """

    def test_named_domains_are_leased_regardless_of_schedule(self, store):
        seed(store, 5)
        # Push it far into the future: it is emphatically not due.
        store.release("site3.com", success=True)
        rec = store.get("site3.com")
        assert rec.next_allowed_at > utcnow()

        leased = store.acquire_domains("w1", ["site3.com"])
        assert [r.domain for r in leased] == ["site3.com"]

    def test_the_scheduled_path_would_not_have_picked_it(self, store):
        """The reason this method exists, stated as a test."""
        seed(store, 5)
        store.release("site3.com", success=True)
        due = store.acquire("w1", limit=10)
        assert "site3.com" not in [r.domain for r in due]

    def test_a_lease_held_by_another_worker_is_still_respected(self, store):
        """It overrides the schedule, never the lease - that is the one thing
        keeping two workers off the same host at once."""
        seed(store, 3)
        assert store.acquire_domains("w1", ["site1.com"])
        assert store.acquire_domains("w2", ["site1.com"]) == []

    def test_targets_come_back_attached(self, store):
        seed(store, 3)
        leased = store.acquire_domains("w1", ["site1.com"])
        assert [t.newsroom_url for t in leased[0].targets] ==             ["https://site1.com/news"]

    def test_unknown_and_duplicate_domains_are_handled(self, store):
        seed(store, 3)
        assert store.acquire_domains("w1", []) == []
        assert store.acquire_domains("w1", ["nope.example"]) == []
        leased = store.acquire_domains("w1", ["site1.com", "site1.com"])
        assert len(leased) == 1

    def test_domains_for_agency(self, store):
        store.upsert_sites([
            (22385, "https://fightcancer.org/press-room", "fightcancer.org"),
            (22385, "https://fightcancer.org/news", "fightcancer.org"),
            (999, "https://other.org/news", "other.org"),
        ])
        assert store.domains_for(a_id=22385) == ["fightcancer.org"]
        assert store.domains_for(a_id=12345) == []

    def test_make_due_brings_one_domain_forward_without_touching_others(self, store):
        seed(store, 4)
        for d in ("site1.com", "site2.com"):
            store.release(d, success=True)
        assert store.make_due(domain="site1.com") == 1
        assert store.get("site1.com").next_allowed_at <= utcnow()
        assert store.get("site2.com").next_allowed_at > utcnow()

    def test_make_due_clears_a_stuck_lease_and_failure_count(self, store):
        seed(store, 2)
        store.acquire_domains("w1", ["site1.com"])
        store.release("site1.com", success=False)
        assert store.get("site1.com").consec_failures == 1

        store.make_due(domain="site1.com")
        rec = store.get("site1.com")
        assert rec.consec_failures == 0
        assert rec.lease_owner is None
        assert store.acquire_domains("w2", ["site1.com"])


class TestScheduleDisabled:
    """`SCRAPEV3_SCHEDULE=off` - the prototype mode.

    The frontier does two separable jobs. Turning the calendar off must not
    touch the lease, because the lease is what makes per-host pacing mean
    anything: two workers on one domain each pace against their own clock.
    """

    def test_a_crawled_domain_stays_due(self, store):
        store.schedule_enabled = False
        seed(store, 3)
        store.acquire_domains("w1", ["site1.com"])
        store.release("site1.com", success=True)
        assert store.get("site1.com").next_allowed_at <= utcnow()
        assert "site1.com" in [r.domain for r in store.acquire("w2", limit=10)]

    def test_with_the_schedule_on_the_same_domain_disappears(self, store):
        """The behaviour being switched off, stated as its own test."""
        store.schedule_enabled = True
        seed(store, 3)
        store.acquire_domains("w1", ["site1.com"])
        store.release("site1.com", success=True)
        assert store.get("site1.com").next_allowed_at > utcnow()
        assert "site1.com" not in [r.domain for r in store.acquire("w2", limit=10)]

    def test_the_lease_still_excludes_a_second_worker(self, store):
        """The one guarantee that must survive. Without it, per-host pacing is
        meaningless however polite each worker is on its own."""
        store.schedule_enabled = False
        seed(store, 2)
        first = store.acquire("w1", limit=2)
        assert first
        assert store.acquire("w2", limit=2) == []

    def test_failures_are_still_counted_they_just_do_not_defer(self, store):
        """`consec_failures` is the health signal the frontier reports on, so
        it keeps counting; it simply stops pushing the domain into the future."""
        store.schedule_enabled = False
        seed(store, 2)
        store.acquire_domains("w1", ["site1.com"])
        store.release("site1.com", success=False)
        rec = store.get("site1.com")
        assert rec.consec_failures == 1
        assert rec.next_allowed_at <= utcnow()

    def test_stats_cannot_contradict_what_a_crawl_would_lease(self, store):
        store.schedule_enabled = False
        seed(store, 5)
        for d in ("site1.com", "site2.com"):
            store.acquire_domains("w1", [d])
            store.release(d, success=True)
        assert store.stats().due == store.stats().enabled

    def test_expired_leases_are_still_reclaimed(self, store):
        store.schedule_enabled = False
        seed(store, 2)
        store.acquire_domains("w1", ["site1.com"], lease_seconds=-1)
        assert store.release_expired_leases() == 1
        assert store.acquire_domains("w2", ["site1.com"])


class TestForgetDiscovery:
    """Resetting a site has to forget where it learned to look.

    The cached source is what makes a solved domain cost one request instead of
    fourteen. It is also why a wrong answer is sticky: fightcancer.org cached
    the organisation-wide /rss.xml and went back to it every run, so the
    cascade never reconsidered.
    """

    def test_the_cached_source_is_cleared(self, store):
        seed(store, 2)
        store.release_target("https://site1.com/news", success=True,
                             discovery_method="rss",
                             feed_url="https://site1.com/rss.xml")
        assert store.targets_for("site1.com")[0].discovery_method == "rss"

        assert store.forget_discovery(domain="site1.com") == 1
        target = store.targets_for("site1.com")[0]
        assert target.discovery_method is None
        assert target.feed_url is None

    def test_conditional_get_state_goes_too(self, store):
        """An unchanged ETag would skip the fetch, so a re-crawl that kept it
        would learn nothing new."""
        seed(store, 2)
        store.release_target("https://site1.com/news", success=True,
                             etag='W/"abc123"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
        store.forget_discovery(domain="site1.com")
        target = store.targets_for("site1.com")[0]
        assert target.etag is None
        assert target.last_modified is None

    def test_the_no_feed_here_verdict_is_cleared(self, store):
        seed(store, 2)
        store.release_target("https://site1.com/news", success=True, feed_absent=True)
        assert store.targets_for("site1.com")[0].feed_absent is True
        store.forget_discovery(domain="site1.com")
        assert store.targets_for("site1.com")[0].feed_absent is False

    def test_other_targets_are_untouched(self, store):
        seed(store, 3)
        for d in ("site1.com", "site2.com"):
            store.release_target(f"https://{d}/news", success=True, discovery_method="rss",
                                 feed_url=f"https://{d}/rss.xml")
        store.forget_discovery(domain="site1.com")
        assert store.targets_for("site1.com")[0].discovery_method is None
        assert store.targets_for("site2.com")[0].discovery_method == "rss"

    def test_scoping_by_agency(self, store):
        store.upsert_sites([(500, "https://a.com/news", "a.com"),
                            (501, "https://b.com/news", "b.com")])
        for u in ("https://a.com/news", "https://b.com/news"):
            store.release_target(u, success=True, discovery_method="sitemap",
                                 feed_url="https://x/sitemap.xml")
        assert store.forget_discovery(a_id=500) == 1
        assert store.targets_for("a.com")[0].discovery_method is None
        assert store.targets_for("b.com")[0].discovery_method == "sitemap"
