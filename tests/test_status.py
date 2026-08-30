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
import re
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
        assert "2 attempts failed" in reason

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

    def test_reasons_are_written_for_a_person_to_read(self):
        # "1 days ago" and "1 crawls in a row" are what an f-string produces and
        # what shipped until the grid was actually looked at in a browser. The
        # reason is the sentence a person reads off the dashboard.
        assert "1 day ago" in _classify(
            last_success_at=NOW - timedelta(days=1))[1]
        assert _classify(last_success_at=NOW)[1] == "crawled successfully today"
        assert "1 crawl in a row" in _classify(
            consec_failures=status.FAILING_AFTER, last_success_at=NOW)[1]             or status.FAILING_AFTER != 1
        assert "3 crawls in a row failed" == _classify(consec_failures=3)[1]
        assert "1 attempt failed" in _classify(
            last_success_at=None, consec_failures=1)[1]

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

    def test_a_partly_solved_agency_does_not_report_as_solved(self, multi, sink):
        # One of two newsrooms solved is not solved. A boolean here would read
        # as done and hide the other one; the count is what makes the gap
        # visible, and `targets_cached < targets` is the condition to branch on.
        multi.release_target("https://x.org/press", success=True,
                             discovery_method="rss")

        row = status.compose(multi, sink)[0]
        assert row.targets == 2
        assert row.targets_cached == 1
        assert row.discovery_method == "rss", "the solved one is still worth showing"

    def test_the_cached_source_travels_with_the_newsroom_that_won(self, multi, sink):
        # A feed URL from the dead newsroom attached to the live one's method is
        # worse than none: it names a source that will never answer again.
        multi._execute(
            "UPDATE target SET feed_url = 'https://x.org/dead.xml', "
            "discovery_method = 'rss' WHERE newsroom_url = 'https://x.org/news'")
        multi.release_target("https://x.org/press", success=True,
                             discovery_method="listing")

        row = status.compose(multi, sink)[0]
        assert row.discovery_method == "listing"
        assert row.feed_url is None
        assert row.targets_cached == 2


class TestTheScheduleTheAgencySees:
    """One agency, two domains, two different paces."""

    @pytest.fixture()
    def spread(self, tmp_path):
        store = SQLiteFrontier(tmp_path / "spread.sqlite")
        store.create_schema()
        store.upsert_sites([
            (600, "https://slow.org/news", "slow.org"),
            (600, "https://fast.org/news", "fast.org"),
        ])
        store._execute(
            "UPDATE domain_state SET next_allowed_at = '2030-01-01 00:00:00', "
            "crawl_delay_s = 30.0 WHERE domain = 'slow.org'")
        store._execute(
            "UPDATE domain_state SET next_allowed_at = '2027-01-01 00:00:00', "
            "crawl_delay_s = 5.0 WHERE domain = 'fast.org'")
        yield store
        store.close()

    def test_next_due_is_the_soonest_of_them(self, spread, sink):
        # When the agency next gets attention, which is when the first of its
        # domains comes up - not the last.
        row = status.compose(spread, sink)[0]
        assert row.next_due_at.year == 2027

    def test_the_crawl_delay_shown_is_the_politest_promise(self, spread, sink):
        # Reporting 5s because one domain allows it would understate what the
        # crawler actually promises the publisher on the other.
        row = status.compose(spread, sink)[0]
        assert row.crawl_delay_s == 30.0


class TestWhenWePulledItAndWhenTheyPublishedIt:
    """Two dates that look interchangeable and are not."""

    def test_they_come_from_different_columns_and_can_disagree(self, frontier, sink):
        # A newsroom republishing a 2019 item: the publisher's date is old, ours
        # is today. Reading `last_article_at` as "when we last pulled something"
        # would call this agency stale while it is working perfectly.
        sink.db.execute(
            "INSERT INTO article (url_hash, content_hash, domain, a_id, url, "
            "headline, published_at, body_len, first_seen_at) "
            "VALUES ('u1', 'c1', 'good.org', 100, 'https://good.org/a', 'h', "
            "'2019-04-01 09:00:00', 900, '2026-08-28 04:00:00')")

        row = next(r for r in status.compose(frontier, sink) if r.a_id == 100)
        assert row.last_article_at.year == 2019, "the publisher's own date"
        assert row.last_stored_at.year == 2026, "when we put it in the index"

    def test_stored_but_never_loaded_is_visible(self, frontier, sink):
        # The third silent failure, after `empty` and `stale`: we reached the
        # site, extraction worked, and nothing reached press_release. Every
        # count above it says this agency is fine.
        _store(sink, 100, "good.org", "https://good.org/a", "2026-08-20 09:00:00")
        _store(sink, 100, "good.org", "https://good.org/b", "2026-08-21 09:00:00")
        sink.db.execute("UPDATE article SET tns_state = 'loaded' "
                        "WHERE url = 'https://good.org/a'")

        row = next(r for r in status.compose(frontier, sink) if r.a_id == 100)
        assert row.articles == 2
        assert row.tns_loaded == 1
        assert row.tns_pending == 1
        assert row.first_stored_at is not None

    def test_a_rejected_article_counts_as_pending_not_loaded(self, frontier, sink):
        # It will never load, and we know why - but it still did not land, and
        # folding it into `tns_loaded` would report success for it.
        _store(sink, 100, "good.org", "https://good.org/a", "2026-08-20 09:00:00")
        sink.db.execute("UPDATE article SET tns_state = 'rejected'")

        row = next(r for r in status.compose(frontier, sink) if r.a_id == 100)
        assert row.tns_loaded == 0
        assert row.tns_pending == 1


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

    def test_every_boolean_is_a_tinyint_in_the_row(self, frontier, sink):
        # Four of them now, and PyMySQL will happily write a Python bool as
        # b'1' into a column that then compares unequal to 1.
        values = dict(zip(status.COLUMNS, status.compose(frontier, sink)[0].as_row()))
        for key in ("enabled", "needs_browser", "feed_absent", "conditional_get"):
            assert values[key] in (0, 1), key

    def test_the_inventory_columns_reach_the_payload(self, frontier, sink):
        # The website reads JSON in development and MySQL in production, so a
        # column that only exists in one of them is a page that works locally.
        payload = json.loads(status.to_json(status.compose(frontier, sink)))
        row = payload["agencies"][0]
        for key in ("targets_cached", "feed_url", "next_due_at", "last_stored_at",
                    "tns_pending", "crawl_delay_s"):
            assert key in row

    def test_the_migration_list_covers_every_column_added_after_the_first_ddl(self):
        # `_DDL` is CREATE TABLE IF NOT EXISTS, so a column present there and
        # absent from `_MIGRATIONS` appears on new deployments and never on the
        # one already running - and nothing raises.
        migrated = {c for c, _ in status._MIGRATIONS}
        original = {
            "a_id", "domain", "newsroom_url", "enabled", "health", "severity",
            "reason", "discovery_method", "targets", "consec_failures",
            "needs_browser", "articles", "articles_recent", "median_body_len",
            "last_success_at", "last_article_at", "updated_at",
        }
        assert set(status.COLUMNS) - original == migrated


class TestOnePageForEveryProducer:
    """The view is a file both the crawler and the website fill in.

    Two renderers - one Python, one PHP - kept in step by hand is the same
    failure as two definitions of "healthy", and it happened: the first pair
    disagreed about which columns existed within a day of being written. So the
    page is `status_view.html`, and every producer does the same substitution.
    """

    def test_the_template_takes_only_a_payload_and_a_note(self):
        # If a third placeholder appears, every producer has to learn to fill
        # it - which is how the duplication starts again.
        template = status.view_template()
        assert set(re.findall(r"__[A-Z]+__", template)) == {"__DATA__", "__NOTE__"}

    def test_rendering_leaves_nothing_unsubstituted(self, frontier, sink):
        page = status.to_html(status.compose(frontier, sink))
        assert not re.findall(r"__[A-Z]+__", page)

    def test_the_page_carries_the_whole_payload_not_just_the_rows(self, frontier, sink):
        # The header is built from `summary` and `generated_at` by the page, so
        # a producer cannot render a header that disagrees with its own rows.
        page = status.to_html(status.compose(frontier, sink))
        embedded = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            page, re.S).group(1)
        grid = json.loads(embedded.replace("<\\/", "</"))
        assert set(grid) == {"generated_at", "summary", "agencies"}
        assert grid["summary"]["recent_days"] == status.RECENT_DAYS

    def test_a_closing_script_tag_in_the_data_cannot_end_the_block(self):
        # A newsroom URL is entirely capable of carrying one, and it would end
        # the JSON block early and render the rest of the payload as markup.
        grid = {"generated_at": "2026-08-30 12:00:00", "summary": {},
                "agencies": [{"a_id": 1, "domain": "x.org",
                              "newsroom_url": "https://x.org/</script><b>"}]}
        page = status.render_view(grid)

        block = page.split('<script id="data" type="application/json">', 1)[1]
        block = block.split("</script>", 1)[0]
        assert "<\\/script>" in block, "the sequence must be inert"
        assert json.loads(block.replace("<\\/", "</"))["agencies"][0]["a_id"] == 1

    def test_the_note_is_escaped_the_same_way(self):
        # It is the other thing substituted in, so it is the other way the block
        # can be ended early.
        page = status.render_view({"agencies": []}, note="see </script> below")
        assert "</script> below" not in page

    def test_a_payload_from_anywhere_renders(self):
        """The point of the whole arrangement: the fetch does not matter.

        This payload never touched the frontier, the sink, or MySQL - it is the
        shape `scrapev3_grid()` returns in PHP, hand-written here.
        """
        grid = {
            "generated_at": "2026-08-30 12:00:00",
            "summary": {"total": 1, "health": {"empty": 1},
                        "severity": {"error": 0, "warn": 1, "ok": 0},
                        "updated_at": "2026-08-30 12:00:00"},
            "agencies": [{"a_id": 7, "domain": "x.org", "health": "empty",
                          "severity": "warn", "targets": 1, "targets_cached": 0}],
        }
        page = status.render_view(grid, note="from the fixture")
        assert "from the fixture" in page
        assert not re.findall(r"__[A-Z]+__", page)


class TestRemovalReachesTheGrid:
    def test_a_removed_agency_leaves_the_grid(self, frontier, sink):
        # Otherwise it keeps its last-known status on the website forever -
        # still listed, still green, and no longer crawled. A removal that does
        # not reach the dashboard is not a removal.
        frontier.remove_agency(200)
        assert {r.a_id for r in status.compose(frontier, sink)} == {100, 300}
