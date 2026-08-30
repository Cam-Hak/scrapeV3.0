"""Adding a site on request, from the other codebase.

The mirror of `test_removal.py`, and the failures worth pinning are the mirror
too. A request that is drained instead of reconciled leaves a site crawled on
one machine and not the other. A request that outranks a removal quietly
resurrects a publisher who asked to be taken out - the same purge undone on
every pass, by design, forever.

MySQL holds the shared list, but every decision here is exercised against a temp
SQLite frontier: by the time it reaches the code that matters, the list is a
sequence of (a_id, url) pairs and a set of removed ids.
"""

from __future__ import annotations

import pytest

from scrapev3 import site_requests
from scrapev3.frontier.store import SQLiteFrontier


@pytest.fixture()
def frontier(tmp_path):
    store = SQLiteFrontier(tmp_path / "frontier.sqlite")
    store.create_schema()
    yield store
    store.close()


def _urls(store) -> set[str]:
    return {r[0] for r in store._execute("SELECT newsroom_url FROM target")}


class TestTheListIsNotAQueue:
    """The rule that makes a second crawler safe."""

    def test_reconciling_twice_leaves_the_same_frontier(self, frontier):
        wanted = [(100, "https://example.org/news")]
        first = site_requests.reconcile(wanted, frontier=frontier)
        second = site_requests.reconcile(wanted, frontier=frontier)

        assert first.seeded == 1
        # Still 1, not 0: the whole list is re-applied every pass, so the count
        # says what the frontier was made to match, not what was new.
        assert second.seeded == 1
        assert _urls(frontier) == {"https://example.org/news"}

    def test_applying_does_not_consume_the_request(self, frontier):
        """Nothing here removes rows from the shared list.

        A queue would, and the second crawler would then never seed the site.
        `reconcile` takes the list as an argument and never writes back to it.
        """
        wanted = [(100, "https://example.org/news"), (200, "https://other.org/news")]
        site_requests.reconcile(wanted, frontier=frontier)
        assert len(wanted) == 2

    def test_re_seeding_does_not_reset_what_the_crawler_learned(self, frontier):
        """A request already in the frontier must cost nothing.

        `upsert_sites` promises this; the request path is the caller most likely
        to break it, because it re-applies the same rows on every single pass.
        A reset `next_allowed_at` would make a requested site jump the queue
        forever, and a lost etag would refetch a page that had not changed.
        """
        site_requests.reconcile([(100, "https://example.org/news")], frontier=frontier)
        frontier._execute(
            "UPDATE target SET etag = 'W/\"abc\"', discovery_method = 'rss' "
            "WHERE newsroom_url = 'https://example.org/news'")
        frontier._execute(
            "UPDATE domain_state SET next_allowed_at = '2099-01-01 00:00:00' "
            "WHERE domain = 'example.org'")

        site_requests.reconcile([(100, "https://example.org/news")], frontier=frontier)

        row = frontier._execute(
            "SELECT etag, discovery_method FROM target "
            "WHERE newsroom_url = 'https://example.org/news'")[0]
        assert row[0] == 'W/"abc"'
        assert row[1] == "rss"
        assert frontier.get("example.org").next_allowed_at.year == 2099


class TestRemovalOutranksARequest:
    """The two tables the website owns, and what happens when they disagree."""

    def test_a_removed_agency_is_not_seeded(self, frontier):
        report = site_requests.reconcile(
            [(100, "https://example.org/news")], frontier=frontier, removed={100})

        assert report.seeded == 0
        assert _urls(frontier) == set()

    def test_the_refusal_is_counted_rather_than_swallowed(self, frontier):
        """A silent skip and no rule at all look identical from the outside."""
        report = site_requests.reconcile(
            [(100, "https://gone.org/news"), (200, "https://ok.org/news")],
            frontier=frontier, removed={100})

        assert report.refused == [100]
        assert report.seeded == 1
        assert report.touched

    def test_one_agencys_removal_does_not_block_another(self, frontier):
        report = site_requests.reconcile(
            [(100, "https://gone.org/news"), (200, "https://ok.org/news")],
            frontier=frontier, removed={100})

        assert _urls(frontier) == {"https://ok.org/news"}
        assert report.refused == [100]


class TestOneBadRowDoesNotStopThePass:
    """The list arrives from a web form, so it will contain junk."""

    def test_an_unusable_url_is_reported_and_the_rest_still_seed(self, frontier):
        report = site_requests.reconcile(
            [(100, "not a url"), (200, "https://ok.org/news")], frontier=frontier)

        assert report.invalid == ["not a url"]
        assert report.seeded == 1
        assert _urls(frontier) == {"https://ok.org/news"}

    def test_a_bare_domain_with_no_scheme_is_refused(self, frontier):
        """Not silently repaired.

        Guessing `https://` for a row somebody typed by hand is the kind of
        helpfulness that ends with the crawler fetching a host nobody meant.
        """
        report = site_requests.reconcile([(100, "example.org")], frontier=frontier)

        assert report.invalid == ["example.org"]
        assert report.seeded == 0

    def test_a_bad_row_is_still_reported_on_the_next_pass(self, frontier):
        """It stays on the list, so the complaint repeats until someone fixes it.

        The alternative - dropping it after one report - means a typo vanishes
        into a log nobody read and the site is never crawled, with no record of
        why.
        """
        wanted = [(100, "not a url")]
        assert site_requests.reconcile(wanted, frontier=frontier).invalid == ["not a url"]
        assert site_requests.reconcile(wanted, frontier=frontier).invalid == ["not a url"]


class TestTheDomainIsDerivedHere:
    """The website sends a URL and an id. The pacing key is ours."""

    def test_the_registrable_domain_becomes_the_pacing_key(self, frontier):
        site_requests.reconcile(
            [(100, "https://news.sub.example.co.uk/press")], frontier=frontier)

        row = frontier._execute("SELECT domain FROM target")[0]
        assert row[0] == "example.co.uk"

    def test_two_newsrooms_on_one_domain_share_it(self, frontier):
        """One lease, one pacing unit - the invariant the whole crawler rests on."""
        site_requests.reconcile(
            [(100, "https://a.house.gov/press"), (200, "https://b.house.gov/press")],
            frontier=frontier)

        domains = frontier._execute("SELECT domain FROM domain_state")
        assert [d[0] for d in domains] == ["house.gov"]

    def test_the_url_is_canonicalised_before_it_is_stored(self, frontier):
        """So the same page requested twice is one target, not two."""
        site_requests.reconcile(
            [(100, "https://Example.ORG/news"), (100, "https://example.org/news")],
            frontier=frontier)

        assert len(_urls(frontier)) == 1


class TestTheReport:
    def test_an_empty_list_touches_nothing(self, frontier):
        report = site_requests.reconcile([], frontier=frontier)

        assert not report.touched
        assert report.seeded == 0
        assert _urls(frontier) == set()
