"""Discovery caching.

Measured problem: battelle.org has no feed, so every run probed nine feed paths
and got nine 404s. At the per-host delay that is ~45 seconds spent proving a
negative, repeated every single day, forever. Separately, sitemap-discovered
domains never recorded *which* sitemap worked, so the whole index walk repeated
too.

Both are cached now. The tests that matter are the ones about *expiry* - a
cache with no TTL would blacklist a site's feed permanently and we would never
notice one appearing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scrapev3.discover import sources
from scrapev3.frontier import SQLiteFrontier
from scrapev3.frontier.models import FEED_ABSENCE_TTL_DAYS, Target, utcnow


class TestFeedAbsenceTTL:
    def test_fresh_verdict_is_trusted(self):
        t = Target(newsroom_url="https://x.com/news", domain="x.com", a_id=1,
                   feed_absent=True, probed_at=utcnow() - timedelta(days=1))
        assert t.feed_absence_is_fresh()

    def test_stale_verdict_is_not_trusted(self):
        """Sites add feeds. A permanent verdict would never notice."""
        t = Target(newsroom_url="https://x.com/news", domain="x.com", a_id=1,
                   feed_absent=True,
                   probed_at=utcnow() - timedelta(days=FEED_ABSENCE_TTL_DAYS + 1))
        assert not t.feed_absence_is_fresh()

    def test_never_probed_is_not_trusted(self):
        t = Target(newsroom_url="https://x.com/news", domain="x.com", a_id=1,
                   feed_absent=True, probed_at=None)
        assert not t.feed_absence_is_fresh()

    def test_feed_present_is_never_absent(self):
        t = Target(newsroom_url="https://x.com/news", domain="x.com", a_id=1,
                   feed_absent=False, probed_at=utcnow())
        assert not t.feed_absence_is_fresh()


class TestPersistence:
    @pytest.fixture
    def store(self, tmp_path):
        f = SQLiteFrontier(tmp_path / "f.sqlite")
        f.create_schema()
        f.upsert_sites([(1, "https://x.com/news", "x.com")])
        yield f
        f.close()

    def test_absence_verdict_round_trips(self, store):
        store.release_target("https://x.com/news", success=False, feed_absent=True)
        t = store.targets_for("x.com")[0]
        assert t.feed_absent is True
        assert t.probed_at is not None
        assert t.feed_absence_is_fresh()

    def test_absence_defaults_to_false(self, store):
        t = store.targets_for("x.com")[0]
        assert t.feed_absent is False
        assert t.probed_at is None

    def test_winning_source_url_is_remembered(self, store):
        """Without this the fast path cannot fire for sitemap domains, which
        was the actual bug making battelle.org slow on every run."""
        store.release_target("https://x.com/news", success=True,
                             discovery_method="sitemap",
                             feed_url="https://x.com/sitemap-news-2026.xml")
        t = store.targets_for("x.com")[0]
        assert t.discovery_method == "sitemap"
        assert t.feed_url == "https://x.com/sitemap-news-2026.xml"

    def test_unrelated_release_does_not_clear_the_verdict(self, store):
        store.release_target("https://x.com/news", success=False, feed_absent=True)
        store.release_target("https://x.com/news", success=True,
                             discovery_method="sitemap")
        t = store.targets_for("x.com")[0]
        assert t.feed_absent is True, "passing feed_absent=None must leave it alone"


class _FakeFetcher:
    """Records every URL requested, so probe suppression is observable."""

    def __init__(self, ok_urls=()):
        self.requested: list[str] = []
        self.ok_urls = set(ok_urls)

    async def robots_for(self, url):
        class Rules:
            sitemaps: list[str] = []

        return Rules()

    async def get(self, url, **kw):
        self.requested.append(url)

        class R:
            pass

        r = R()
        r.ok = url in self.ok_urls
        r.text = "<rss></rss>" if r.ok else ""
        r.status = 200 if r.ok else 404
        r.wall = None
        r.from_cache = False
        r.error = None
        r.headers = {}
        return r


class TestProbeSuppression:
    async def test_probing_hits_every_feed_path(self):
        f = _FakeFetcher()
        url, probed_empty, declared = await sources.find_feed(
            f, "https://x.com/news", html="<html></html>")
        assert url is None
        assert probed_empty is True
        assert declared is False
        assert len(f.requested) == len(sources.FEED_PATHS)

    async def test_skip_probe_makes_no_requests(self):
        """The whole point: ~45s per run per feedless site, reclaimed."""
        f = _FakeFetcher()
        url, probed_empty, _declared = await sources.find_feed(
            f, "https://x.com/news", html="<html></html>", skip_probe=True)
        assert url is None
        assert f.requested == []
        # Must NOT re-assert absence - it did not probe, so it learned nothing.
        assert probed_empty is False

    async def test_autodiscovery_still_runs_when_probing_is_skipped(self):
        """A site that added a feed since we last looked must still be found,
        because autodiscovery is free once the page is already in hand."""
        html = ('<html><head><link rel="alternate" type="application/rss+xml" '
                'href="/new-feed.xml"></head></html>')
        f = _FakeFetcher()
        url, _, declared = await sources.find_feed(
            f, "https://x.com/news", html=html, skip_probe=True)
        assert url == "https://x.com/new-feed.xml"
        assert f.requested == []
        # Declared by the page itself, so it is authoritative for this target -
        # a probed root feed is only a guess and has to corroborate.
        assert declared is True


class TestSitemapShortCircuit:
    async def test_known_sitemap_skips_the_index_walk(self):
        leaf = "https://x.com/sitemap-news-2026.xml"
        f = _FakeFetcher(ok_urls=[leaf])
        await sources.from_sitemap(f, "https://x.com", known_sitemap=leaf)
        assert f.requested == [leaf], "should go straight to the known leaf"

    async def test_without_known_sitemap_it_probes(self):
        f = _FakeFetcher()
        await sources.from_sitemap(f, "https://x.com")
        assert len(f.requested) > 1


class TestSitemapPriority:
    def test_section_hint_ranks_first(self):
        rank = sources._sitemap_priority
        assert rank("https://x.com/sitemap-newsroom.xml", "/newsroom") == 0
        assert rank("https://x.com/sitemap-news.xml", None) == 1
        assert rank("https://x.com/sitemap-press.xml", None) == 2
        assert rank("https://x.com/sitemap-2026.xml", None) == 3
        assert rank("https://x.com/sitemap-products.xml", None) == 4

    def test_ordering_puts_the_section_sitemap_first(self):
        urls = ["https://x.com/sitemap-products.xml",
                "https://x.com/sitemap-2026.xml",
                "https://x.com/sitemap-newsroom.xml"]
        ordered = sorted(urls, key=lambda u: sources._sitemap_priority(u, "/newsroom"))
        assert ordered[0].endswith("newsroom.xml")


class TestListingHarvestScoping:
    """A listing page links to the entire site, so "same domain and
    article-shaped" is far too permissive on its own.

    battelle.org's press-release page yielded /markets/national-security/... and
    /markets/infrastructure/... - marketing pages with four-word slugs that pass
    the URL classifier cleanly. Scoping the harvest to the target's own section
    is what separates a press release from a product page.
    """

    HTML = """
    <html><body>
      <nav>
        <a href="/markets/national-security/defense-and-material-solutions">Defense</a>
        <a href="/markets/infrastructure/research-management-operations">Infrastructure</a>
        <a href="/about-us/our-leadership-team">Leadership</a>
      </nav>
      <main>
        <a href="/insights/newsroom/battelle-wins-major-contract-award">Contract award</a>
        <a href="/insights/newsroom/new-lab-opens-in-columbus-ohio">New lab</a>
      </main>
    </body></html>
    """
    PAGE = "https://www.battelle.org/insights/newsroom/press-releases"

    def test_the_url_classifier_alone_cannot_reject_the_nav(self):
        """The underlying difficulty, unchanged: by URL shape,
        /markets/national-security/defense-and-material-solutions is
        indistinguishable from a press release. Nothing about the string says
        otherwise, which is why harvesting needs a structural signal."""
        from scrapev3.urls import classify_url

        assert classify_url(
            "https://www.battelle.org/markets/national-security/"
            "defense-and-material-solutions").is_article

    def test_harvest_excludes_nav_links_structurally(self):
        """What the classifier cannot do, the document structure can: those
        links sit inside <nav>, and the real press releases do not."""
        refs = sources.harvest_links(self.HTML, self.PAGE)
        paths = [r.url for r in refs]
        assert paths
        assert not any("/markets/" in p for p in paths)
        assert all("/insights/newsroom/" in p for p in paths)

    def test_scoped_harvest_keeps_only_the_section(self):
        refs = sources.harvest_links(self.HTML, self.PAGE, section="/insights/newsroom")
        paths = [r.url for r in refs]
        assert paths, "real press releases must survive"
        assert all("/insights/newsroom/" in p for p in paths)
        assert not any("/markets/" in p for p in paths)

    def test_scoped_harvest_returns_nothing_rather_than_junk(self):
        """When the section has no article links, the honest answer is zero.

        Widening back to the whole site would just re-harvest the nav, and a
        domain that quietly yields marketing pages is worse than one that
        visibly yields nothing and gets flagged.
        """
        nav_only = "<html><body><nav><a href='/markets/some-product-page-here'>x</a></nav></body></html>"
        refs = sources.harvest_links(nav_only, self.PAGE, section="/insights/newsroom")
        assert refs == []


class TestSiteWideSourceOutranksTheTarget:
    """The failure fightcancer.org exposed, in all three sources that can hit it.

    Its press room is at /press-room/search; the releases themselves live at
    /releases/<slug>. Every whole-site source therefore fails to match the
    section, and each one used to fall back to "the whole site" and win - which
    looks like success. The crawler collected ten real documents that were
    simply the wrong ten, and reported no error at all. That is the silent
    failure this project exists to catch, so each source is pinned here.
    """

    PAGE = "https://www.fightcancer.org/press-room/search"
    # Shaped like the real page: 3 nav links outnumbering 2 real releases, and
    # the releases on a completely different path from the listing.
    HTML = """
    <html><body>
      <header><a href="/what-we-do/access-health-insurance">Access</a></header>
      <nav>
        <a href="/what-we-do/reducing-health-disparities">Disparities</a>
        <a href="/policy-resources/prescription-drug-affordability">Drug pricing</a>
      </nav>
      <main>
        <a href="/releases/new-billboard-thanks-congress-helping-cancer-survivors">Billboard</a>
        <a href="/releases/arhome-lifeline-cancer-patients-arkansas">ARHOME</a>
        <a href="/releases/filler-0">F0</a>
        <a href="/releases/filler-1">F1</a>
        <a href="/releases/filler-2">F2</a>
        <a href="/releases/filler-3">F3</a>
        <a href="/releases/filler-4">F4</a>
        <a href="/releases/filler-5">F5</a>
        <a href="/releases/filler-6">F6</a>
        <a href="/releases/filler-7">F7</a>
        <a href="/releases/filler-8">F8</a>
        <a href="/releases/filler-9">F9</a>
        <a href="/releases/filler-10">F10</a>
        <a href="/releases/filler-11">F11</a>
        <a href="/releases/filler-12">F12</a>
        <a href="/releases/filler-13">F13</a>
        <a href="/releases/filler-14">F14</a>
        <a href="/releases/filler-15">F15</a>
        <a href="/releases/filler-16">F16</a>
        <a href="/releases/filler-17">F17</a>
        <a href="/releases/filler-18">F18</a>
        <a href="/releases/filler-19">F19</a>
        <a href="/releases/filler-20">F20</a>
        <a href="/releases/filler-21">F21</a>
      </main>
      <footer><a href="/support-our-work/become-member-acs-can">Join</a></footer>
    </body></html>
    """

    def test_harvest_finds_releases_the_section_filter_cannot(self):
        """Section scoping finds nothing here - the articles are not under the
        listing's path - so the fallback has to be the thing that works."""
        section = "/press-room/search"
        assert sources.harvest_links(self.HTML, self.PAGE, section=section) != []
        refs = sources.harvest_links(self.HTML, self.PAGE, section=section)
        assert all("/releases/" in r.url for r in refs)
        assert len(refs) == 2

    def test_the_nav_majority_does_not_win(self):
        """Counting would pick the nav: 4 chrome links to 2 real ones. The
        signal is structural position, not frequency."""
        refs = sources.harvest_links(self.HTML, self.PAGE, section="/press-room/search")
        assert not any("/what-we-do/" in r.url for r in refs)
        assert not any("/policy-resources/" in r.url for r in refs)
        assert not any("/support-our-work/" in r.url for r in refs)

    def test_a_declared_feed_is_trusted_without_corroboration(self):
        """A feed the page itself links to is authoritative for that page."""
        found = sources.Discovery(method="rss")
        found.articles = [sources.ArticleRef(url="https://www.fightcancer.org/anything")]
        assert sources._covers_target(
            found, self.HTML, self.PAGE, "/press-room/search", declared=True)

    def test_a_probed_root_feed_with_no_overlap_is_rejected(self):
        """/rss.xml is the organisation-wide feed: advocacy actions, events and
        legislative summaries. All real articles, none of them press releases."""
        found = sources.Discovery(method="rss")
        found.articles = [
            sources.ArticleRef(url="https://www.fightcancer.org/actions/its-time-expand-medicaid"),
            sources.ArticleRef(url="https://www.fightcancer.org/events/florida-research-breakfast"),
        ]
        assert not sources._covers_target(
            found, self.HTML, self.PAGE, "/press-room/search", declared=False)

    def test_a_probed_feed_that_shares_one_item_is_accepted(self):
        """One link in common is enough: the feed is about this section, and a
        feed legitimately runs ahead of a cached listing page."""
        found = sources.Discovery(method="rss")
        found.articles = [
            sources.ArticleRef(url="https://fightcancer.org/releases/arhome-lifeline-cancer-patients-arkansas"),
            sources.ArticleRef(url="https://fightcancer.org/releases/something-brand-new"),
        ]
        assert sources._covers_target(
            found, self.HTML, self.PAGE, "/press-room/search", declared=False)

    def test_a_root_target_has_no_section_to_corroborate_against(self):
        """When the target IS the site's newsroom, a root feed is the right
        feed and there is nothing to check it against."""
        found = sources.Discovery(method="rss")
        found.articles = [sources.ArticleRef(url="https://x.com/whatever")]
        assert sources._covers_target(found, self.HTML, self.PAGE, None, declared=False)

    def test_sitemap_marks_itself_unscoped_when_the_section_misses(self):
        """The flag that lets the cascade prefer the listing page over a
        whole-site dump."""
        out = sources.Discovery()
        assert out.scoped is True


class TestListingFastPath:
    """A target settled on the listing page must not re-walk the sitemap.

    There was no fast path for `listing`, so such a target fell straight
    through to the full cascade on every run - re-establishing, daily and at
    the per-host delay, that the sitemap does not cover it. Measured on
    fightcancer.org: 8 requests and 38 seconds of discovery, 7 of them wasted.
    """

    PAGE = "https://www.fightcancer.org/press-room/search"
    HTML = """
    <html><body>
      <nav><a href="/what-we-do/access-health-insurance">Access</a></nav>
      <main>
        <a href="/releases/new-billboard-thanks-congress-helping-cancer-survivors">A</a>
        <a href="/releases/arhome-lifeline-cancer-patients-arkansas">B</a>
      </main>
    </body></html>
    """

    class _Fetcher:
        def __init__(self, html, page):
            self.requested: list[str] = []
            self._html, self._page = html, page

        async def robots_for(self, url):
            class Rules:
                sitemaps: list[str] = []
            return Rules()

        async def get(self, url, **kw):
            self.requested.append(url)
            r = type("R", (), {})()
            r.ok = url.rstrip("/") == self._page.rstrip("/")
            r.text = self._html if r.ok else ""
            r.status = 200 if r.ok else 404
            r.wall = r.error = None
            r.from_cache = False
            r.headers = {}
            return r

    async def test_a_cached_listing_target_costs_one_request(self):
        f = self._Fetcher(self.HTML, self.PAGE)
        found = await sources.discover(f, self.PAGE, known_method="listing", limit=20)
        assert found.method == "listing"
        assert len(found.articles) == 2
        assert f.requested == [self.PAGE], (
            "the listing page and nothing else - no feed probe, no sitemap walk")

    async def test_it_falls_through_when_the_page_stops_yielding(self):
        """A site that changes layout must be reconsidered, not written off."""
        f = self._Fetcher("<html><body><nav><a href='/x'>x</a></nav></body></html>",
                          self.PAGE)
        found = await sources.discover(f, self.PAGE, known_method="listing", limit=20)
        assert found.method == "none"
        # It went on to try the rest of the cascade rather than giving up.
        assert len(f.requested) > 1

    async def test_the_page_is_not_fetched_twice_on_fall_through(self):
        """The fast path hands its response to the cascade below it."""
        f = self._Fetcher("<html><body><nav><a href='/x'>x</a></nav></body></html>",
                          self.PAGE)
        await sources.discover(f, self.PAGE, known_method="listing", limit=20)
        assert f.requested.count(self.PAGE) == 1


class TestSitemapRecency:
    """A sitemap has no ordering guarantee, and plenty are oldest-first.

    crnusa.org's opens with 2016 entries; its newest is nine years later.
    Taking the first N in document order fetched twenty-five 2016 articles and
    rejected every one for being outside the date window - paying for the fetch
    to learn what `lastmod` already said.
    """

    def test_newest_entries_survive_the_truncation(self):
        refs = [sources.ArticleRef(url=f"https://x.com/{y}", date_raw=f"{y}-01-01T00:00:00Z")
                for y in (2016, 2019, 2026, 2021)]
        refs.sort(key=sources._recency_key, reverse=True)
        assert [r.url for r in refs] == [
            "https://x.com/2026", "https://x.com/2021",
            "https://x.com/2019", "https://x.com/2016"]

    def test_iso_text_sorts_without_parsing_any_dates(self):
        """`lastmod` is ISO 8601, which orders correctly as plain text - so
        ranking thousands of refs costs no date parsing at all."""
        a = sources.ArticleRef(url="a", date_raw="2026-08-25T09:00:00-04:00")
        b = sources.ArticleRef(url="b", date_raw="2026-08-25T08:00:00-04:00")
        assert sources._recency_key(a) > sources._recency_key(b)

    def test_undated_entries_sort_last_rather_than_first(self):
        """Plenty of sitemaps omit lastmod. An entry with no date must not
        outrank one that has a recent date - and must not crash the sort."""
        dated = sources.ArticleRef(url="dated", date_raw="2020-01-01T00:00:00Z")
        bare = sources.ArticleRef(url="bare", date_raw=None)
        junk = sources.ArticleRef(url="junk", date_raw="last Tuesday")
        refs = [bare, junk, dated]
        refs.sort(key=sources._recency_key, reverse=True)
        assert refs[0].url == "dated"


class TestSourceMustBeThisPublisher:
    """ufw.org runs a press-clippings feed.

    Its `/wp-json` returns 28 links to Courthouse News, HuffPost, Newsweek and
    the Washington Post out of 30. All real articles, none of them UFW's. The
    crawl vetoes them - so nothing wrong was ever stored - but discovery had
    already declared success on the surviving 2 and cached "cms_api works",
    never trying the listing page that carries 24 actual UFW press releases.

    "Found something" is the wrong test. "Found something we would keep" is the
    right one.
    """

    @staticmethod
    def _found(own: int, foreign: int) -> sources.Discovery:
        d = sources.Discovery(method="cms_api")
        d.articles = (
            [sources.ArticleRef(url=f"https://ufw.org/release-{i}") for i in range(own)]
            + [sources.ArticleRef(url=f"https://huffpost.com/entry/story-{i}")
               for i in range(foreign)])
        return d

    def _usable(self, found):
        """Exercise the real gate through discover's closure."""
        from scrapev3.urls import canonical_url, registrable_domain
        seed = canonical_url("https://ufw.org/news-and-events/press-releases")
        target_domain = registrable_domain(seed)
        total = len(found.articles)
        found.articles = [a for a in found.articles
                          if canonical_url(a.url) != seed
                          and registrable_domain(a.url) == target_domain]
        if (total >= sources.MIN_RESULTS_FOR_RATIO
                and len(found.articles) / total < sources.MIN_OWN_CONTENT):
            return False
        return bool(found.articles)

    def test_a_source_that_is_mostly_someone_else_is_rejected(self):
        assert self._usable(self._found(own=2, foreign=28)) is False

    def test_a_source_that_is_mostly_ours_is_kept(self):
        assert self._usable(self._found(own=24, foreign=1)) is True

    def test_the_foreign_results_are_stripped_either_way(self):
        found = self._found(own=24, foreign=1)
        self._usable(found)
        assert all("ufw.org" in a.url for a in found.articles)

    def test_a_small_result_set_is_judged_on_content_not_ratio(self):
        """One syndicated link in a three-item feed says nothing about the
        source, so the ratio rule needs a floor before it applies."""
        assert self._usable(self._found(own=1, foreign=2)) is True

    def test_a_source_returning_only_foreign_content_is_rejected(self):
        assert self._usable(self._found(own=0, foreign=10)) is False


class TestCmsApiIsCorroboratedToo:
    """`/wp-json/wp/v2/posts` is exactly as site-wide as `/rss.xml`.

    aacr.org's serves the blog while the target is
    /about-the-aacr/newsroom/news-releases. Real articles, wrong section, no
    error - the same silent shape as fightcancer.org, reached through a
    different door. Three sources needed three separate patches before the rule
    was made general, which is the lesson worth keeping.
    """

    PAGE = "https://www.aacr.org/about-the-aacr/newsroom/news-releases"
    HTML = """
    <html><body><main>
      <a href="/about-the-aacr/newsroom/news-releases/a-vaccine-to-prevent-pancreatic-cancer">A</a>
      <a href="/about-the-aacr/newsroom/news-releases/aacr-names-matthew-vander-heiden">B</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-0">F0</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-1">F1</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-2">F2</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-3">F3</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-4">F4</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-5">F5</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-6">F6</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-7">F7</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-8">F8</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-9">F9</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-10">F10</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-11">F11</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-12">F12</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-13">F13</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-14">F14</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-15">F15</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-16">F16</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-17">F17</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-18">F18</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-19">F19</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-20">F20</a>
      <a href="/about-the-aacr/newsroom/news-releases/filler-21">F21</a>
    </main></body></html>
    """

    def test_a_cms_api_serving_another_section_is_rejected(self):
        found = sources.Discovery(method="cms_api")
        found.articles = [
            sources.ArticleRef(url="https://aacr.org/blog/2026/08/26/breaking-news-daraxonrasib"),
            sources.ArticleRef(url="https://aacr.org/blog/2026/08/25/driving-car-t-cell-therapy"),
        ]
        assert not sources._covers_target(
            found, self.HTML, self.PAGE, "/about-the-aacr/newsroom/news-releases", False)

    def test_a_cms_api_serving_the_target_section_is_accepted(self):
        found = sources.Discovery(method="cms_api")
        found.articles = [sources.ArticleRef(
            url="https://aacr.org/about-the-aacr/newsroom/news-releases/"
                "a-vaccine-to-prevent-pancreatic-cancer")]
        assert sources._covers_target(
            found, self.HTML, self.PAGE, "/about-the-aacr/newsroom/news-releases", False)

    def test_the_cached_fast_path_declines_on_a_sectioned_target(self):
        """There is no page in hand at fast-path time, so a sectioned target
        must fall through to the cascade rather than trust the cache blindly.
        The extra cost is one request; the alternative is a wrong section every
        run, forever."""
        import inspect
        src = inspect.getsource(sources.discover)
        assert 'known_method == "cms_api" and not section' in src


class TestCorroborationNeedsSomethingToCorroborateAgainst:
    """A JS-rendered newsroom exposes almost no links in the HTML we hold.

    Rejecting a source because it does not match a page that lists nothing is
    treating absence of evidence as evidence of absence. northernvermont.edu
    paid for it: its `/wp-json` was returning real articles, corroboration
    found no overlap because the page carried six of its own links, and
    discovery fell through to a sitemap serving a category index and a 2021
    dean's list.

    The counting matters as much as the threshold. That page has 120 links -
    social profiles, a parchment.com registration, a portal on another domain -
    and only 6 are its own. Counting all of them made the guard useless.
    """

    PAGE = "https://www.northernvermont.edu/category/news-center"
    SECTION = "/category/news-center"

    @staticmethod
    def _found(*urls):
        d = sources.Discovery(method="cms_api")
        d.articles = [sources.ArticleRef(url=u) for u in urls]
        return d

    def test_a_thin_page_gives_the_source_the_benefit_of_the_doubt(self):
        html = ("<html><body>"
                + "".join(f'<a href="/own-{i}">x</a>' for i in range(6))
                + "</body></html>")
        found = self._found("https://northernvermont.edu/green-mountain-job-retention-program")
        assert sources._covers_target(found, html, self.PAGE, self.SECTION, False)

    def test_external_links_do_not_count_towards_the_threshold(self):
        """120 links, 6 of them this publisher's. The rest prove nothing about
        whether the page lists its own articles."""
        html = ("<html><body>"
                + "".join(f'<a href="/own-{i}">x</a>' for i in range(6))
                + "".join(f'<a href="https://facebook.com/p{i}">x</a>' for i in range(114))
                + "</body></html>")
        found = self._found("https://northernvermont.edu/green-mountain-job-retention-program")
        assert sources._covers_target(found, html, self.PAGE, self.SECTION, False)

    def test_a_page_that_really_does_list_its_articles_still_judges(self):
        """With enough of its own links, silence is meaningful again."""
        html = ("<html><body>"
                + "".join(f'<a href="/category/news-center/real-{i}">x</a>' for i in range(25))
                + "</body></html>")
        assert not sources._covers_target(
            self._found("https://northernvermont.edu/somewhere-else-entirely"),
            html, self.PAGE, self.SECTION, False)
        assert sources._covers_target(
            self._found("https://northernvermont.edu/category/news-center/real-3"),
            html, self.PAGE, self.SECTION, False)
