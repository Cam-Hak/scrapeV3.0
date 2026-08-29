"""Per-agency health, as the website's grid reads it.

The grid is the only view most people will ever have of whether the crawler is
working, so a wrong verdict here is worse than no verdict: it is a green dot on
a site that has been silently storing nothing for a month.

Two things are worth pinning. The classification rules, which are pure and
decide what a person sees. And the fact that the *website* never re-derives
them - it renders `severity`, which is a closed vocabulary, so a health word
added later cannot make the grid fall over.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scrapev3 import status
from scrapev3.frontier.store import SQLiteFrontier
from scrapev3.sink import Sink

NOW = datetime(2026, 8, 29, 12, 0, 0)


def _classify(**overrides):
    kwargs = dict(enabled=True, consec_failures=0, needs_browser=False,
                  last_success_at=NOW - timedelta(days=1),
                  last_article_at=NOW - timedelta(days=2),
                  articles=5, now=NOW)
    kwargs.update(overrides)
    return status.classify(**kwargs)


@pytest.fixture()
def frontier(tmp_path):
    store = SQLiteFrontier(tmp_path / "frontier.sqlite")
    store.create_schema()
    store.upsert_sites([
        (100, "https://good.org/news", "good.org"),
        (200, "https://quiet.org/news", "quiet.org"),
        (300, "https://broken.org/news", "broken.org"),
    ])
    yield store
    store.close()


@pytest.fixture()
def sink(tmp_path):
    s = Sink(tmp_path)
    yield s
    s.close()


def _store(sink: Sink, a_id: int, domain: str, url: str, published: str) -> None:
    """Insert straight into the dedup index - the grid only reads it."""
    sink.db.execute(
        "INSERT INTO article (url_hash, content_hash, domain, a_id, url, "
        "headline, published_at, body_len, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, f"c{url}", domain, a_id, url, "h", published, 900, published))


class TestTheRulesPeopleActuallySee:
    """`classify` is pure, and its ORDER is the substance.

    Each rule below outranks the ones after it for a reason, and a reordering
    would not fail any single-condition test - only these.
    """

    def test_a_working_site_is_healthy(self):
        assert _classify()[0] == "healthy"

    def test_a_disabled_agency_says_so_rather_than_looking_broken(self):
        # Outranks everything: a site nobody crawls has no crawl to judge, and
        # reporting it as `failing` would send someone to fix a working site.
        health, _ = _classify(enabled=False, consec_failures=99,
                              last_success_at=None)
        assert health == "disabled"

    def test_never_crawled_outranks_the_failure_count(self):
        # Zero successes out of two attempts is not a streak of failures, and
        # calling it `failing` implies something regressed.
        health, reason = _classify(last_success_at=None, consec_failures=2)
        assert health == "never"
        assert "2 attempt(s) failed" in reason

    def test_failing_outranks_stale(self):
        # Staleness is the SYMPTOM of a failing crawl. Reporting the symptom
        # sends someone to look at the schedule instead of at the site.
        health, _ = _classify(consec_failures=status.FAILING_AFTER,
                              last_success_at=NOW - timedelta(days=400))
        assert health == "failing"

    def test_one_bad_crawl_is_not_a_failure(self):
        # A single timeout must not light the grid red; the frontier retries
        # with a doubling backoff and blips clear on their own.
        assert _classify(consec_failures=status.FAILING_AFTER - 1)[0] == "healthy"

    def test_a_site_we_cannot_render_is_blocked_not_failing(self):
        # We reach it fine. The fix is a browser, not a network problem.
        assert _classify(needs_browser=True)[0] == "blocked"

    def test_no_news_is_not_broken(self):
        # The distinction the whole severity scheme exists for: institutional
        # newsrooms go months between releases, and a grid that calls that
        # `failing` cries wolf on most of the corpus.
        health, reason = _classify(
            last_article_at=NOW - timedelta(days=status.QUIET_AFTER_DAYS + 1))
        assert health == "quiet"
        assert status.severity_of(health) == "ok", "quiet must not read as a fault"
        assert "crawling fine" in reason

    def test_reaching_a_site_and_storing_nothing_is_its_own_fault(self):
        # NOT `stale`. Stale means we stopped reaching the site; this means we
        # reach it and nothing survives to storage - the silent failure this
        # project exists to surface, and the largest bucket in the first real
        # run (92 of 324 agencies crawled).
        health, reason = _classify(articles=0, last_article_at=None)
        assert health == "empty"
        assert status.severity_of(health) == "warn"
        assert "no article was ever stored" in reason

    def test_a_site_we_stopped_reaching_is_stale(self):
        health, reason = _classify(
            last_success_at=NOW - timedelta(days=status.STALE_AFTER_DAYS + 1))
        assert health == "stale"
        assert "days ago" in reason

    def test_every_health_word_has_a_severity(self):
        # The website switches on severity. A health word with no band would
        # render uncoloured and read as "fine".
        for health in ("healthy", "quiet", "disabled", "stale", "blocked",
                       "empty", "never", "failing"):
            assert status.severity_of(health) in ("ok", "warn", "error")

    def test_an_unknown_health_word_warns_rather_than_passing(self):
        # A new fault added to the crawler must not appear green on a website
        # that has not been redeployed. Failing safe means warning.
        assert status.severity_of("some-new-fault") == "warn"


class TestComposingFromTheStores:
    def test_a_never_crawled_agency_still_gets_a_row(self, frontier, sink):
        # It must appear on the grid as `never`, not be missing from it - an
        # absent row is indistinguishable from an agency nobody seeded.
        rows = {r.a_id: r for r in status.compose(frontier, sink)}
        assert set(rows) == {100, 200, 300}
        assert rows[100].health == "never"
        assert rows[100].severity == "error"

    def test_articles_and_recency_come_from_the_index(self, frontier, sink):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recent = (now - timedelta(days=2)).isoformat(timespec="seconds")
        old = (now - timedelta(days=status.RECENT_DAYS + 10)).isoformat(
            timespec="seconds")
        _store(sink, 100, "good.org", "https://good.org/1", recent)
        _store(sink, 100, "good.org", "https://good.org/2", old)
        frontier.release_target("https://good.org/news", success=True)

        row = {r.a_id: r for r in status.compose(frontier, sink)}[100]
        assert row.articles == 2
        assert row.articles_recent == 1, "a total alone never goes down"
        assert row.health == "healthy"

    def test_a_crawled_site_storing_nothing_shows_as_empty(self, frontier, sink):
        frontier.release_target("https://broken.org/news", success=True)
        row = {r.a_id: r for r in status.compose(frontier, sink)}[300]
        assert row.health == "empty"

    def test_stored_articles_count_as_a_successful_crawl(self, frontier, sink):
        # No release_target call at all - the state an interrupted pass leaves,
        # because articles are written as they are extracted while the target is
        # marked at the end. aacom.org sat like this after one real run.
        #
        # Reporting "never crawled successfully" about an agency whose articles
        # are sitting in the index is a contradiction the person reading the
        # grid cannot resolve, so the stored article is taken as the evidence
        # it is.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _store(sink, 100, "good.org", "https://good.org/1",
               (now - timedelta(days=1)).isoformat(timespec="seconds"))

        row = {r.a_id: r for r in status.compose(frontier, sink)}[100]
        assert frontier.targets_for("good.org")[0].last_success_at is None
        assert row.health == "healthy"
        assert row.last_success_at is not None

    def test_articles_without_an_agency_are_not_bucketed_under_a_fake_id(
            self, frontier, sink):
        _store(sink, None, "good.org", "https://good.org/orphan", "2026-08-01")
        counts = sink.article_stats(since=datetime(2026, 1, 1))
        assert None not in counts and 0 not in counts
        assert {r.a_id for r in status.compose(frontier, sink)} == {100, 200, 300}


class TestOneAgencyWithSeveralNewsrooms:
    """Rare - 2 of 2399 - but the aggregation must not invent numbers."""

    @pytest.fixture()
    def multi(self, tmp_path):
        store = SQLiteFrontier(tmp_path / "multi.sqlite")
        store.create_schema()
        store.upsert_sites([
            (500, "https://x.org/news", "x.org"),
            (500, "https://x.org/press", "x.org"),
        ])
        yield store
        store.close()

    def test_failures_are_maxed_not_summed(self, multi, sink):
        # Two independent per-target streaks of 2 is not a streak of 4. Summing
        # would report a number that never happened and cross FAILING_AFTER on
        # a site that never failed three times in a row.
        for url in ("https://x.org/news", "https://x.org/press"):
            for _ in range(2):
                multi.release_target(url, success=False)

        rows = status.compose(multi, sink)
        assert len(rows) == 1, "one row per agency, not per newsroom URL"
        assert rows[0].targets == 2
        assert rows[0].consec_failures == 2
        assert rows[0].health != "failing"

    def test_the_live_newsroom_wins_over_a_stale_one(self, multi, sink):
        multi.release_target("https://x.org/press", success=True,
                             discovery_method="rss")
        rows = status.compose(multi, sink)
        assert rows[0].discovery_method == "rss"
        assert rows[0].newsroom_url == "https://x.org/press"


class TestThePayloadTheWebsiteReads:
    def test_json_matches_the_columns_published_to_mysql(self, frontier, sink):
        rows = status.compose(frontier, sink)
        payload = json.loads(status.to_json(rows))
        assert len(payload["agencies"]) == 3
        # Same header shape as clients/status.php's scrapev3_summary(), so a
        # page written against the fixture survives being pointed at MySQL.
        assert payload["summary"]["total"] == 3
        assert payload["summary"]["health"] == {"never": 3}
        assert payload["summary"]["severity"] == {"error": 3, "warn": 0, "ok": 0}
        assert "updated_at" in payload["summary"]
        # The fixture the demo page renders and the table the site queries have
        # to carry the same fields, or a page written against one breaks on the
        # other.
        assert set(status.COLUMNS) <= set(payload["agencies"][0])

    def test_timestamps_survive_json(self, frontier, sink):
        frontier.release_target("https://good.org/news", success=True)
        rows = status.compose(frontier, sink)
        row = next(r for r in rows if r.a_id == 100)
        assert row.last_success_at is not None
        assert row.as_dict()["last_success_at"].startswith("20")

    def test_a_row_orders_its_values_to_match_the_column_list(self, frontier, sink):
        # `as_row` feeds a positional executemany; a field reordered in the
        # dataclass without COLUMNS following would write values into the wrong
        # columns and never raise.
        row = status.compose(frontier, sink)[0]
        values = dict(zip(status.COLUMNS, row.as_row()))
        assert values["a_id"] == row.a_id
        assert values["domain"] == row.domain
        assert values["health"] == row.health
        assert values["enabled"] in (0, 1), "MySQL wants a TINYINT, not a bool"


class TestRemovalReachesTheGrid:
    def test_a_removed_agency_leaves_the_grid(self, frontier, sink):
        # Otherwise it keeps its last-known status on the website forever -
        # still listed, still green, and no longer crawled. A removal that does
        # not reach the dashboard is not a removal.
        frontier.remove_agency(200)
        assert {r.a_id for r in status.compose(frontier, sink)} == {100, 300}
