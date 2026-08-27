"""Tests for URL canonicalization, eTLD+1, and article/listing classification.

These are not incidental utilities. `registrable_domain` is both the politeness
pacing key and the shard key - if it is wrong we either hammer a publisher or
split one publisher across two workers, and neither failure is loud.
"""

import pytest

from scrapev3.urls import (
    canonical_url,
    classify_url,
    is_non_news_path,
    make_site,
    registrable_domain,
    url_hash,
)


class TestRegistrableDomain:
    def test_simple(self):
        assert registrable_domain("https://www.example.com/news") == "example.com"

    def test_subdomain_collapses(self):
        # The whole point: these are one server and one blast radius.
        assert registrable_domain("https://news.example.com/x") == "example.com"
        assert registrable_domain("https://www.example.com/y") == "example.com"

    def test_multipart_suffixes(self):
        # Naive "last two labels" gets every one of these wrong, and the v2
        # corpus is full of them (115 .ca/.uk domains alone).
        assert registrable_domain("https://www.bbc.co.uk/news") == "bbc.co.uk"
        assert registrable_domain("https://www3.nhk.or.jp/news/") == "nhk.or.jp"

    def test_gov_uk_is_itself_a_public_suffix(self):
        """`gov.uk` is a public suffix, so www.gov.uk IS the registrable domain.

        This looks wrong at a glance and is correct. It also happens to be the
        behaviour we want for pacing: everything under www.gov.uk is one host
        and must be paced as one unit. A department on its own subdomain still
        separates correctly.
        """
        assert registrable_domain("https://www.gov.uk/news") == "www.gov.uk"
        assert registrable_domain("https://hmrc.gov.uk/news") == "hmrc.gov.uk"

    def test_edu_and_gov(self):
        # 635 .edu and 632 .gov domains in the reference corpus.
        assert registrable_domain("https://news.mit.edu/") == "mit.edu"
        assert registrable_domain("https://www.af.mil/news/") == "af.mil"

    def test_bare_host_input(self):
        assert registrable_domain("news.example.com") == "example.com"

    def test_case_and_trailing_dot(self):
        assert registrable_domain("https://News.EXAMPLE.com./x") == "example.com"

    def test_empty_is_empty_not_crash(self):
        assert registrable_domain("") == ""


class TestCanonicalUrl:
    def test_strips_fragment(self):
        assert canonical_url("https://e.com/a#top") == "https://e.com/a"

    def test_strips_tracking_params(self):
        got = canonical_url("https://e.com/a?utm_source=x&utm_medium=y&id=7")
        assert got == "https://e.com/a?id=7"

    def test_strips_fbclid_and_gclid(self):
        assert canonical_url("https://e.com/a?fbclid=z") == "https://e.com/a"
        assert canonical_url("https://e.com/a?gclid=z") == "https://e.com/a"

    def test_sorts_query(self):
        assert canonical_url("https://e.com/a?b=2&a=1") == "https://e.com/a?a=1&b=2"

    def test_normalizes_case_and_default_port(self):
        assert canonical_url("HTTPS://E.com:443/A") == "https://e.com/A"
        assert canonical_url("http://e.com:80/a") == "http://e.com/a"

    def test_keeps_nondefault_port(self):
        assert canonical_url("https://e.com:8443/a") == "https://e.com:8443/a"

    def test_strips_trailing_slash_but_keeps_root(self):
        assert canonical_url("https://e.com/a/") == "https://e.com/a"
        assert canonical_url("https://e.com/") == "https://e.com/"

    def test_collapses_double_slashes(self):
        assert canonical_url("https://e.com//a//b") == "https://e.com/a/b"

    def test_resolves_relative_against_base(self):
        got = canonical_url("/news/story", base="https://e.com/section/index.html")
        assert got == "https://e.com/news/story"

    def test_syndication_tags_collapse_to_same_key(self):
        """Press releases syndicate with per-outlet campaign tags."""
        a = url_hash("https://e.com/pr/widget-launch?utm_campaign=outlet-a")
        b = url_hash("https://e.com/pr/widget-launch?utm_campaign=outlet-b")
        assert a == b

    def test_hash_is_stable_and_differs(self):
        assert url_hash("https://e.com/a") == url_hash("https://e.com/a/")
        assert url_hash("https://e.com/a") != url_hash("https://e.com/b")


class TestClassifyUrl:
    def test_dated_article_path(self):
        v = classify_url("https://e.com/2026/08/25/mayor-announces-new-transit-plan")
        assert v.is_article, v.reasons

    def test_hyphenated_slug_is_article(self):
        v = classify_url("https://e.com/news/city-council-approves-budget-increase")
        assert v.is_article, v.reasons

    def test_category_page_is_listing(self):
        assert not classify_url("https://e.com/category/politics").is_article

    def test_tag_page_is_listing(self):
        assert not classify_url("https://e.com/tag/elections").is_article

    def test_author_page_is_listing(self):
        assert not classify_url("https://e.com/author/jane-doe").is_article

    def test_pagination_is_listing(self):
        assert not classify_url("https://e.com/news/page/2").is_article
        assert not classify_url("https://e.com/news?page=3").is_article

    def test_bare_section_is_listing(self):
        assert not classify_url("https://e.com/news").is_article

    def test_numeric_permalink_is_article(self):
        v = classify_url("https://e.com/articles/48210")
        assert v.is_article, v.reasons

    def test_reasons_are_populated(self):
        v = classify_url("https://e.com/2026/08/25/some-long-story-slug-here")
        assert v.reasons


class TestMakeSite:
    def test_derives_keys(self):
        site = make_site("https://News.Example.com/newsroom/")
        assert site.domain == "example.com"
        assert site.host == "news.example.com"
        assert site.origin == "https://news.example.com"
        assert site.newsroom_url == "https://news.example.com/newsroom"

    def test_adds_scheme_via_canonical(self):
        site = make_site("https://example.com/press")
        assert site.newsroom_url.startswith("https://")


class TestSectionIndexRejection:
    """Production bug: battelle.org's own newsroom URL was classified as an
    article and stored with the site's nav text as its body.

    Root cause: a 3-segment path scored +0.5 for "deep path" with no positive
    article signal, and 0.5 > 0 passed. Now a positive signal is required.
    """

    @pytest.mark.parametrize("url", [
        "https://www.battelle.org/insights/newsroom/press-releases",
        "https://x.com/news",
        "https://x.com/newsroom",
        "https://x.com/about/press",
        "https://x.com/media-center",
        "https://x.com/company/news-releases",
        "https://x.com/insights/blog",
        "https://x.com/latest",
    ])
    def test_section_indexes_are_not_articles(self, url):
        assert not classify_url(url).is_article, classify_url(url).reasons

    @pytest.mark.parametrize("url", [
        "https://www.battelle.org/insights/newsroom/battelle-wins-major-contract",
        "https://x.com/newsroom/some-real-story-about-things",
        "https://x.com/2026/08/20/mayor-announces-transit-plan",
        "https://x.com/articles/48210",
        "https://x.com/press/company-reports-record-quarterly-earnings",
    ])
    def test_real_articles_under_those_sections_still_pass(self, url):
        assert classify_url(url).is_article, classify_url(url).reasons

    def test_deep_path_alone_is_not_enough(self):
        """Path depth is not evidence of articleness - it was the whole bug."""
        v = classify_url("https://x.com/a/b/c")
        assert not v.is_article
        assert "no positive article signal" in v.reasons

    def test_positive_signal_is_reported(self):
        v = classify_url("https://x.com/a/b/a-real-story-slug-here")
        assert v.is_article
        assert any("slug has" in r for r in v.reasons)


class TestSlugThreshold:
    """Three words, not four.

    Production data forced this: falmouth.ac.uk publishes real articles at
    slugs like "rewriting-story-metal" (3 words), while the section indexes we
    must reject are 1-2 words ("press-releases", "media-center", "all-news").
    Three separates them cleanly; four produced false negatives on real stories.
    """

    def test_three_word_slug_is_an_article(self):
        assert classify_url("http://www.falmouth.ac.uk/news/rewriting-story-metal").is_article

    def test_two_word_section_index_is_not(self):
        assert not classify_url("https://x.com/news/press-releases").is_article
        assert not classify_url("https://x.com/about/media-center").is_article
        assert not classify_url("https://x.com/all-news").is_article


class TestNonNewsContent:
    """Institutional sites often run ONE site-wide feed mixing press releases
    with events, staff profiles and course pages.

    Production case: edisonohio.edu's /News feed yielded
    /event/2026-08/welcome-week - a campus event. It passed every other check
    because it is genuinely article-shaped (dated path, hyphenated slug), and
    feeds bypass the article/listing classifier since feeds normally only carry
    articles. So this veto applies to every source, feeds included.
    """

    @pytest.mark.parametrize("url", [
        "https://www.edisonohio.edu/event/2026-08/welcome-week",
        "https://x.com/events/annual-conference-2026",
        "https://x.com/calendar/2026/08/open-day",
        "https://x.com/people/jane-doe-professor",
        "https://x.com/staff/john-smith-director",
        "https://x.com/courses/intro-to-molecular-biology",
        "https://x.com/programs/master-of-public-health",
        "https://x.com/careers/senior-software-engineer",
        "https://x.com/locations/downtown-campus-map",
    ])
    def test_non_news_sections_are_vetoed(self, url):
        assert is_non_news_path(url)
        assert not classify_url(url).is_article

    @pytest.mark.parametrize("url", [
        "https://x.com/news/2026/08/mayor-announces-transit-plan",
        "https://x.com/press/company-wins-major-award",
        "https://x.com/newsroom/quarterly-earnings-report-released",
    ])
    def test_real_news_is_not_vetoed(self, url):
        assert not is_non_news_path(url)
        assert classify_url(url).is_article

    def test_veto_outweighs_a_date_in_the_path(self):
        """A dated event URL must still be rejected - the date is what made
        the edisonohio case look legitimate."""
        v = classify_url("https://x.com/event/2026-08/welcome-week")
        assert not v.is_article
        assert "non-news section" in v.reasons


class TestSoftNonNewsSections:
    """Broad institutional sections are usually not news, but not always.

    edisonohio.edu/about/edison-foundation/1879-society reached the store: it
    is article-shaped enough (3 segments, hyphenated slug) to pass every other
    check, and /about/ was not on the hard veto list. Blanket-vetoing /about/
    would break sites that publish at /about/news/..., so this is a soft veto
    that a news segment anywhere on the path rescues.
    """

    @pytest.mark.parametrize("url", [
        "https://www.edisonohio.edu/about/edison-foundation/1879-society",
        "https://www.edisonohio.edu/about/edison-foundation/august-make-will-month",
        "https://x.com/about-us/our-leadership-team",
        "https://x.com/foundation/annual-giving-report",
        "https://x.com/alumni/class-notes-summer-edition",
    ])
    def test_static_institutional_pages_are_vetoed(self, url):
        assert is_non_news_path(url)
        assert not classify_url(url).is_article

    @pytest.mark.parametrize("url", [
        "https://x.com/about/news/mayor-visits-the-campus",
        "https://x.com/about-us/press-releases/company-wins-award",
        "https://x.com/foundation/news/major-gift-announced-today",
    ])
    def test_news_segment_rescues_the_soft_veto(self, url):
        assert not is_non_news_path(url)
        assert classify_url(url).is_article

    def test_hard_veto_is_not_rescued_by_a_news_segment(self):
        """An events page under /news/ is still an event."""
        assert is_non_news_path("https://x.com/news/events/annual-gala-2026")


class TestWwwNormalisation:
    """`www.` is decorative on almost every site, and treating it as meaningful
    stored the same article twice. Found by the discovery audit: crnusa.org's
    sitemap emits the bare host while its newsroom page links the www one."""

    def test_www_and_bare_host_collapse_to_one_key(self):
        a = canonical_url("https://www.crnusa.org/newsroom/crn-begins-new-year")
        b = canonical_url("https://crnusa.org/newsroom/crn-begins-new-year")
        assert a == b == "https://crnusa.org/newsroom/crn-begins-new-year"
        assert url_hash(a) == url_hash(b)

    def test_www_gov_uk_is_left_alone(self):
        """`gov.uk` is a public suffix, so `www.gov.uk` IS the registrable
        domain. Stripping there would turn a real site into a suffix belonging
        to nobody."""
        assert canonical_url("https://www.gov.uk/news") == "https://www.gov.uk/news"

    def test_other_subdomains_are_untouched(self):
        """Only `www.` is decorative. `newsroom.` and `www3.` are real hosts."""
        for host in ("newsroom.northumbria.ac.uk", "www3.nhk.or.jp", "media.srpnet.com"):
            assert host in canonical_url(f"https://{host}/x")
