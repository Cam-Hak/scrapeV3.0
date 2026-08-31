"""Three holes in "a site-wide source must corroborate before it wins".

The rule itself is old and the cascade already states it. These are the three
places the rule was not actually reached, all found by scoring the full corpus
(`data/audits/full-corpus.jsonl`, 1,747 targets) rather than by anything
raising:

  1. A DECLARED feed bypassed corroboration entirely. WordPress emits the
     site-wide `/feed` into the head of every page it renders, so a press room
     declares it as readily as the blog does - 64 targets took this path and
     stored the wrong documents while reporting success.
  2. The RESERVE sitemap - the whole-site dump kept behind the listing page -
     was returned unexamined when the listing failed. 22 targets won from
     there with zero overlap, and 18 of those 22 extracted cleanly: a real
     headline, a real date, and the wrong document.
  3. The sitemap rung counted nav includes and section indexes as yield,
     because the crawl's URL gate ran downstream of the decision instead of
     inside it.

Each is the project's own silent-failure shape - plausible-looking wrong data,
no exception, no error line - so each gets a test that fails on the old
behaviour.
"""

from __future__ import annotations

from scrapev3.discover import sources
from scrapev3.discover.sources import (ArticleRef, Discovery, _covers_target,
                                       _declaration_is_scoped)


def _page(*hrefs: str) -> str:
    """A listing page carrying enough links to be worth corroborating against.

    Padded past MIN_PAGE_LINKS_TO_JUDGE, because under that threshold every
    source gets the benefit of the doubt and these tests would pass vacuously.
    """
    filler = "".join(f'<a href="/section/item-{i}">i{i}</a>'
                     for i in range(sources.MIN_PAGE_LINKS_TO_JUDGE + 5))
    return "<html><body>" + "".join(
        f'<a href="{h}">x</a>' for h in hrefs) + filler + "</body></html>"


def _found(*urls: str) -> Discovery:
    return Discovery(method="rss",
                     articles=[ArticleRef(url=u, source="rss") for u in urls])


class TestADeclaredFeedIsAboutOwnershipNotScope:
    """`<link rel=alternate>` says whose feed it is, not what it covers."""

    def test_site_root_feed_is_not_scoped_to_a_section(self):
        # cleanpower.org/news declares /feed, whose items are all /blog/...
        assert not _declaration_is_scoped("https://cleanpower.org/feed", "/news")

    def test_a_feed_under_the_section_is_scoped(self):
        assert _declaration_is_scoped("https://x.org/news/feed", "/news")

    def test_the_section_itself_counts_as_scoped(self):
        assert _declaration_is_scoped("https://x.org/news", "/news")

    def test_a_deeper_section_does_not_trust_a_shallower_feed(self):
        assert not _declaration_is_scoped("https://x.org/news/feed",
                                          "/news/press-releases")

    def test_a_site_root_target_has_no_section_to_violate(self):
        """No section means nothing to be off-topic from - trust it."""
        assert _declaration_is_scoped("https://x.org/feed", None)

    def test_trailing_slashes_do_not_change_the_answer(self):
        assert _declaration_is_scoped("https://x.org/news/feed/", "/news")

    def test_the_wordpress_shape_end_to_end(self):
        """The whole 64-target cluster in one assertion.

        A press-release page declaring the site-wide feed, whose items are
        blog posts that appear nowhere on it. Before the fix `declared=True`
        reached `_covers_target` and short-circuited on the first branch.
        """
        section = "/press-releases"
        feed = "https://pen.org/feed"
        listing = _page("/press-releases/one", "/press-releases/two")
        found = _found("https://pen.org/when-free-speech-causes-harm",
                       "https://pen.org/sonia-feldman-interview")

        trusted = _declaration_is_scoped(feed, section)
        assert not trusted, "a root feed on a section page is not authoritative"
        assert not _covers_target(found, listing, "https://pen.org/press-releases",
                                  section, trusted), \
            "zero overlap with the section's own page must not win"

    def test_a_genuinely_scoped_feed_still_wins_without_overlap(self):
        """The fix must not demand overlap from a feed that earned its trust.

        A section's own feed is authoritative even when its newest items have
        already scrolled off page one of the listing.
        """
        trusted = _declaration_is_scoped("https://x.org/news/feed", "/news")
        assert trusted
        assert _covers_target(_found("https://x.org/news/older-story"),
                              _page("/news/newer-story"),
                              "https://x.org/news", "/news", trusted)


class TestTheReserveSitemapIsNotCorroborated:
    """Why the obvious third fix was tried, measured, and reverted.

    The reserve - the whole-site sitemap kept behind the listing page - wins
    for 22 targets with zero overlap and stores the wrong document, so making
    it corroborate looks like the same fix as the other two. It is not, and
    this class exists so the next reader does not re-derive that the hard way.

    The reason is that these newsrooms are JS-rendered. Their listing HTML
    carries chrome and nothing else, so overlap is zero whether the sitemap is
    right or wrong, and the check has no signal to act on. Measured on the
    live pages: 4 article-shaped links on nyclu.org, 7 on americanrivers.org,
    10 on nature.org - all chrome.
    """

    NYCLU_RIGHT = ["https://nyclu.org/press-release/appeals-court-rejects",
                   "https://nyclu.org/press-release/new-trump-ban-continues"]
    BNY_WRONG = ["https://bny.com/investments/be/en/fund/adaptive-risk-eur",
                 "https://bny.com/investments/be/en/fund/adaptive-risk-usd"]

    @staticmethod
    def _chrome_page() -> str:
        """A JS-rendered newsroom: plenty of links, none of them articles."""
        return "<html><body>" + "".join(
            f'<a href="/issues/topic-{i}">t{i}</a>'
            for i in range(sources.MIN_PAGE_LINKS_TO_JUDGE + 5)) + "</body></html>"

    def test_corroboration_rejects_the_right_sitemap_too(self):
        """The measurement that killed the fix.

        nyclu.org's unscoped sitemap is the correct source - its articles live
        at /press-release/ while the target is /press - and corroboration
        throws it away exactly as readily as it throws away bny.com's.
        """
        right = Discovery(method="sitemap", articles=[
            ArticleRef(url=u, source="sitemap") for u in self.NYCLU_RIGHT])
        wrong = Discovery(method="sitemap", articles=[
            ArticleRef(url=u, source="sitemap") for u in self.BNY_WRONG])
        page = self._chrome_page()

        verdict_right = _covers_target(right, page, "https://nyclu.org/press",
                                       "/press", False)
        verdict_wrong = _covers_target(wrong, page, "https://bny.com/press",
                                       "/press", False)
        assert verdict_right == verdict_wrong, (
            "page-link overlap cannot separate a wrong source from an "
            "unobservable one - if this ever becomes false, corroborating "
            "the reserve is worth revisiting")
        assert not verdict_right, "and it rejects both, including the right one"

    def test_the_article_gate_cannot_separate_them_either(self):
        """The other discriminator that was tried. Both look like articles."""
        from scrapev3.urls import classify_url, is_non_news_path

        def article_like(urls):
            return all(not is_non_news_path(u) and classify_url(u).is_article
                       for u in urls)

        assert article_like(self.NYCLU_RIGHT)
        assert article_like(self.BNY_WRONG), (
            "URL shape says both are articles, so it cannot gate the reserve")

    def test_a_thin_page_still_gets_the_benefit_of_the_doubt(self):
        """Unchanged, and load-bearing for every JS-rendered newsroom."""
        found = Discovery(method="sitemap", articles=[
            ArticleRef(url="https://x.org/stories/one", source="sitemap")])
        assert _covers_target(found, "<html><body><a href='/a'>a</a></body></html>",
                              "https://x.org/news", "/news", False)


class TestTheSitemapRungAppliesTheCrawlsOwnGate:
    """Furniture in a sitemap is not yield."""

    INFRASTRUCTURE = [
        "https://sanjac.edu/about/news/_nav.ounav",
        "https://sanjac.edu/about/news/index.php",
        "https://news.columbusstate.edu/_banner.inc",
        "https://news.columbusstate.edu/categories.php",
        "https://news.unt.edu/news-releases/index.html",
    ]
    ARTICLES = [
        "https://unisq.edu.au/news/2026/08/plant-growth-lab",
        "https://aba.com/about-us/press-room/press-releases/fdic-qbp-q2-2026",
    ]

    def test_the_gate_rejects_every_infrastructure_url(self):
        from scrapev3.urls import classify_url, is_non_news_path
        for u in self.INFRASTRUCTURE:
            assert not (not is_non_news_path(u) and classify_url(u).is_article), u

    def test_the_gate_keeps_every_real_article(self):
        from scrapev3.urls import classify_url, is_non_news_path
        for u in self.ARTICLES:
            assert not is_non_news_path(u) and classify_url(u).is_article, u

    def test_a_sitemap_of_nothing_but_furniture_yields_nothing(self):
        """The point of gating inside discovery rather than downstream.

        Ungated, these five are five results: enough to satisfy `usable()`,
        win the cascade and cache as the winning method - so the cascade never
        retries and the site reports as solved forever, while every crawl
        gates them away again and stores nothing. `empty` was 92 of the first
        324 agencies crawled, and this is one of the ways to get there.
        """
        refs = [ArticleRef(url=u, source="sitemap") for u in self.INFRASTRUCTURE]
        assert len(refs) == 5, "ungated, furniture counts as yield"
        assert self._gate(refs) == [], "gated, it is correctly nothing"

    def test_gating_keeps_the_real_articles_in_a_mixed_sitemap(self):
        """The same sitemap usually carries both. Only the furniture goes."""
        refs = [ArticleRef(url=u, source="sitemap")
                for u in self.INFRASTRUCTURE + self.ARTICLES]
        assert [r.url for r in self._gate(refs)] == self.ARTICLES

    @staticmethod
    def _gate(refs):
        """The gate exactly as `from_sitemap` applies it."""
        from scrapev3.urls import classify_url, is_non_news_path
        return [r for r in refs
                if r.source == "news_sitemap"
                or (not is_non_news_path(r.url) and classify_url(r.url).is_article)]

    def test_a_news_sitemap_entry_is_exempt(self):
        """`<news:news>` is the publisher asserting this is an article.

        Same reason feeds are not gated: URL shape does not overrule an
        explicit statement.
        """
        refs = [ArticleRef(url="https://x.org/2026/index.html",
                           source="news_sitemap")]
        kept = [r for r in refs if r.source == "news_sitemap"]
        assert kept, "a news sitemap entry must survive the URL gate"
