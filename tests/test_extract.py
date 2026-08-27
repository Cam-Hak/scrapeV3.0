"""Extraction cascade tests.

Several of these encode findings measured on the real corpus during the Phase 1
survey, and several are regression tests for specific v2 failures. Both kinds
are labelled, because the reason a test exists is the part worth keeping.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from scrapev3.extract import (
    ARTICLE_TYPES,
    clean_body,
    date_from_url,
    detect_language,
    extract_article,
    extract_jsonld,
    extract_meta,
    headline_from_dom,
    headline_from_title,
    looks_like_navigation,
    prose_ratio,
    parse_date_string,
    token_overlap,
)
from scrapev3.extract.cascade import needs_browser
from scrapev3.extract.body import best_prose_ratio
from scrapev3.extract.dates import resolve_date
from scrapev3.extract.models import Path
from selectolax.lexbor import LexborHTMLParser


def page(head: str = "", body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


LONG = (
    "<p>The mayor today announced a sweeping overhaul of the transit network, "
    "promising new bus rapid transit corridors and expanded light rail service "
    "over the coming decade at considerable public expense.</p>"
    "<p>Funding will come from a mix of federal grants and a local bond measure "
    "that voters approved last November. Officials said construction could begin "
    "as early as next spring, weather permitting.</p>"
    "<p>Critics questioned the timeline and warned that cost overruns have "
    "plagued similar projects in other cities across the wider region.</p>"
)


class TestJsonLdTypes:
    """Survey finding: NewsArticle 12, Article 12, BlogPosting 2.

    Matching only `NewsArticle` - the obvious choice - would have missed 48% of
    the article-type JSON-LD in the sample.
    """

    @pytest.mark.parametrize("typ", ["NewsArticle", "Article", "BlogPosting", "Report"])
    def test_all_four_article_types_are_recognised(self, typ):
        html = page(f'<script type="application/ld+json">'
                    f'{{"@type":"{typ}","headline":"H","datePublished":"2026-08-20"}}</script>')
        facts = extract_jsonld(LexborHTMLParser(html))
        assert facts.is_article
        assert facts.headline == "H"

    def test_type_list_form(self):
        html = page('<script type="application/ld+json">'
                    '{"@type":["WebPage","NewsArticle"],"headline":"H"}</script>')
        assert extract_jsonld(LexborHTMLParser(html)).is_article

    def test_graph_is_flattened(self):
        html = page('<script type="application/ld+json">'
                    '{"@graph":[{"@type":"Organization"},'
                    '{"@type":"NewsArticle","headline":"Deep"}]}</script>')
        assert extract_jsonld(LexborHTMLParser(html)).headline == "Deep"

    def test_malformed_block_does_not_abort_extraction(self):
        html = page('<script type="application/ld+json">{not json}</script>'
                    '<script type="application/ld+json">'
                    '{"@type":"NewsArticle","headline":"Survived"}</script>')
        assert extract_jsonld(LexborHTMLParser(html)).headline == "Survived"

    def test_webpage_supplies_date_but_is_marked_weak(self):
        """Survey finding: article types ~24% of pages, WebPage ~40%.

        WebPage inherits datePublished from CreativeWork, so it is a legitimate
        date source - but it does not assert the page is an article.
        """
        html = page('<script type="application/ld+json">'
                    '{"@type":"WebPage","name":"Some page","datePublished":"2026-08-20"}</script>')
        facts = extract_jsonld(LexborHTMLParser(html))
        assert facts.is_article is False
        assert facts.weak_only is True
        assert facts.date_raw == "2026-08-20"

    def test_article_type_beats_webpage(self):
        html = page('<script type="application/ld+json">'
                    '{"@type":"WebPage","name":"Nav title","datePublished":"2020-01-01"}</script>'
                    '<script type="application/ld+json">'
                    '{"@type":"NewsArticle","headline":"Real","datePublished":"2026-08-20"}</script>')
        facts = extract_jsonld(LexborHTMLParser(html))
        assert facts.headline == "Real"
        assert facts.weak_only is False


class TestHeadline:
    def test_prefers_jsonld_over_og(self):
        html = page('<meta property="og:title" content="OG title">'
                    '<script type="application/ld+json">'
                    '{"@type":"NewsArticle","headline":"LD title"}</script>', LONG)
        a = extract_article(html, "https://x.com/a")
        assert a.headline == "LD title"
        assert a.headline_source is Path.JSONLD

    def test_falls_back_to_og_then_h1(self):
        html = page('<meta property="og:title" content="OG title">', LONG)
        assert extract_article(html, "https://x.com/a").headline == "OG title"

        html2 = page("", "<h1>Just an H1 headline here</h1>" + LONG)
        assert extract_article(html2, "https://x.com/a").headline == "Just an H1 headline here"

    def test_title_site_suffix_is_trimmed(self):
        tree = LexborHTMLParser(page("<title>Mayor announces transit plan | City News</title>"))
        assert headline_from_dom(tree) == "Mayor announces transit plan"

    def test_title_kept_when_no_separator(self):
        tree = LexborHTMLParser(page("<title>A perfectly ordinary headline</title>"))
        assert headline_from_dom(tree) == "A perfectly ordinary headline"

    def test_headline_not_duplicated_as_first_body_line(self):
        """Extractors include the <h1>; storing it twice is wrong and also
        destroys 'headline == first body line' as a broken-extraction signal."""
        html = page("<title>T</title>", "<article><h1>Mayor announces transit plan</h1>"
                    + LONG + "</article>")
        a = extract_article(html, "https://x.com/a")
        assert a.body
        assert not a.body.lower().startswith("mayor announces transit plan")


class TestDates:
    def test_iso_with_offset(self):
        assert parse_date_string("2026-08-20T14:30:00Z") == datetime(2026, 8, 20, 14, 30)

    def test_rejects_pre_1995(self):
        assert parse_date_string("1970-01-01") is None

    def test_rejects_far_future(self):
        assert parse_date_string("2099-01-01") is None

    def test_url_path_dates(self):
        assert date_from_url("https://x.com/2026/08/20/slug") == datetime(2026, 8, 20)
        assert date_from_url("https://x.com/20260820/slug") == datetime(2026, 8, 20)
        assert date_from_url("https://x.com/no-date-here") is None

    def test_jsonld_beats_footer_copyright(self):
        """v2 regression: its date logic routinely grabbed page chrome."""
        html = page('<script type="application/ld+json">'
                    '{"@type":"NewsArticle","headline":"H","datePublished":"2026-08-20"}</script>',
                    LONG + "<footer>Copyright 2011 Some Publisher</footer>")
        a = extract_article(html, "https://x.com/a")
        assert a.date.value == datetime(2026, 8, 20)
        assert a.date.source is Path.JSONLD

    def test_opengraph_published_time_is_used(self):
        html = page('<meta property="article:published_time" content="2026-07-04T09:00:00Z">', LONG)
        a = extract_article(html, "https://x.com/a")
        assert a.date.value == datetime(2026, 7, 4, 9, 0)
        assert a.date.source is Path.OPENGRAPH

    def test_feed_date_outranks_everything(self):
        html = page('<meta property="article:published_time" content="2026-07-04T09:00:00Z">', LONG)
        a = extract_article(html, "https://x.com/a", feed_date="2026-07-01T00:00:00Z")
        assert a.date.source is Path.FEED

    def test_weak_jsonld_date_is_demoted_below_htmldate(self):
        html = page('<script type="application/ld+json">'
                    '{"@type":"WebPage","datePublished":"2019-01-01"}</script>'
                    '<meta property="article:published_time" content="2026-07-04T09:00:00Z">',
                    LONG)
        a = extract_article(html, "https://x.com/a")
        # OpenGraph still wins outright, and the stale WebPage date must not.
        assert a.date.value == datetime(2026, 7, 4, 9, 0)

    def test_disagreement_is_flagged_not_hidden(self):
        html = page('<script type="application/ld+json">'
                    '{"@type":"NewsArticle","datePublished":"2026-08-20"}</script>', LONG)
        a = extract_article(html, "https://x.com/2020/01/01/old-slug")
        assert a.date.disagreement_days and a.date.disagreement_days > 7
        assert any("disagree" in w for w in a.warnings)

    def test_missing_date_is_reported(self):
        a = extract_article(page("", LONG), "https://x.com/a")
        if a.date.value is None:
            assert "no date" in a.warnings


class TestBodyCleaning:
    def test_does_not_collapse_paragraph_breaks(self):
        """v2 regression, applied to 100% of its documents:

            re.sub(r"\\s*\\.", ".", body)

        `\\s` includes `\\n`, so any sentence starting after a paragraph break
        had that break silently eaten.
        """
        text = "First paragraph ends here.\n\n.Next starts oddly.\n\nThird one."
        cleaned = clean_body(text)
        assert "\n\n" in cleaned
        assert cleaned.count("\n\n") >= 2

    def test_strips_press_release_furniture(self):
        text = "FOR IMMEDIATE RELEASE\n\n" + "Body text here. " * 30 + "\n\n###"
        cleaned = clean_body(text)
        assert "FOR IMMEDIATE RELEASE" not in cleaned
        assert "###" not in cleaned

    def test_truncates_at_related_stories(self):
        body = "Real article text. " * 30
        cleaned = clean_body(body + "\nRelated\nSome other headline\nAnother one")
        assert "Some other headline" not in cleaned

    def test_does_not_truncate_a_sentence_starting_with_related(self):
        body = "Related agencies have been consulted throughout the process. " * 10
        assert clean_body(body).startswith("Related agencies")

    def test_collapses_blank_line_runs(self):
        assert clean_body("a\n\n\n\n\nb") == "a\n\nb"

    def test_empty_input(self):
        assert clean_body(None) is None
        assert clean_body("   ") is None


class TestTokenOverlap:
    def test_identical(self):
        assert token_overlap("the cat sat", "the cat sat") == 1.0

    def test_disjoint(self):
        assert token_overlap("alpha beta", "gamma delta") == 0.0

    def test_empty_is_zero_not_crash(self):
        assert token_overlap(None, "x") == 0.0
        assert token_overlap("", "") == 0.0


class TestBrowserEscalation:
    """Escalate on OUTCOME, never on framework markers."""

    def test_server_rendered_next_js_does_not_escalate(self):
        html = '<div id="__next">' + "x" * 2000 + "</div>"
        escalate, _ = needs_browser(html, "a" * 500, False)
        assert escalate is False

    def test_empty_shell_escalates(self):
        escalate, reason = needs_browser("<html><body></body></html>", None, False)
        assert escalate is True
        assert reason

    def test_hydration_payload_is_called_out_as_mineable(self):
        html = "<html><body>" + "x" * 2000 + "<script>window.__NEXT_DATA__={}</script></body></html>"
        escalate, reason = needs_browser(html, "", False)
        assert escalate is True
        assert "hydration" in reason

    def test_structured_body_prevents_escalation(self):
        escalate, _ = needs_browser("<html></html>", None, True)
        assert escalate is False


class TestEndToEnd:
    def test_full_article(self):
        html = page(
            "<title>Mayor announces transit plan | City News</title>"
            '<meta property="og:site_name" content="City News">'
            '<script type="application/ld+json">'
            '{"@type":"NewsArticle","headline":"Mayor announces transit plan",'
            '"datePublished":"2026-08-20T14:30:00Z"}</script>',
            "<nav>Home About Contact</nav><article><h1>Mayor announces transit plan</h1>"
            + LONG + "</article><footer>Copyright 2011</footer>",
        )
        a = extract_article(html, "https://citynews.com/2026/08/20/mayor-transit-plan")
        assert a.headline == "Mayor announces transit plan"
        assert a.date.value == datetime(2026, 8, 20, 14, 30)
        assert a.body_len >= 300
        assert a.usable
        assert a.warnings == []
        assert "Home About Contact" not in a.body
        assert "Copyright" not in a.body
        assert a.quality["headline_in_body"] is not None

    def test_empty_html_is_handled(self):
        a = extract_article("", "https://x.com/a")
        assert not a.usable
        assert "empty html" in a.warnings

    def test_quality_records_every_source(self):
        a = extract_article(page("", LONG), "https://x.com/a")
        for key in ("headline_source", "body_source", "date_source", "body_len"):
            assert key in a.quality


class TestTitleChromeStripping:
    """Production bug: centerforfoodsafety.org yielded the headline

        "Center for Food Safety | Press Releases | | Lawsuit Filed to Stop..."

    Splitting on the LAST separator assumes "Headline | Site". Plenty of sites
    use "Site | Section | Headline", where that rule keeps exactly the wrong
    half. Now: split on every separator, drop site name and section chrome,
    keep the longest survivor.
    """

    def test_site_name_last(self):
        assert headline_from_title(
            "Mayor announces transit plan | City News") == "Mayor announces transit plan"

    def test_site_name_first_with_section(self):
        got = headline_from_title(
            "Center for Food Safety | Press Releases | | Lawsuit Filed to Stop Pollution",
            site_name="Center for Food Safety")
        assert got == "Lawsuit Filed to Stop Pollution"

    def test_site_name_first_without_hint(self):
        got = headline_from_title(
            "Acme Corp | Newsroom | Acme reports record quarterly earnings")
        assert got == "Acme reports record quarterly earnings"

    def test_dash_separator(self):
        assert headline_from_title(
            "Council approves the budget increase - Example Times"
        ) == "Council approves the budget increase"

    def test_no_separator_is_returned_whole(self):
        assert headline_from_title("A perfectly ordinary headline") == "A perfectly ordinary headline"

    def test_all_chrome_falls_back_to_the_title(self):
        assert headline_from_title("News | Press Releases") == "News | Press Releases"


class TestLanguageDetection:
    """Replaces v2's 19-word Spanish/French substring blocklist.

    Also a regression test for a silent failure of my own: the fast_langdetect
    API changed shape between releases, the call raised TypeError, and a bare
    `except Exception` swallowed it - so every article came back with
    language=None and nothing indicated anything was wrong.
    """

    def test_detects_english(self):
        text = ("The mayor announced a sweeping overhaul of the city transit "
                "network today, promising new rapid transit corridors.")
        assert detect_language(text) == "en"

    def test_detects_portuguese(self):
        text = ("Banco Mundial apoia reforma fiscal e crescimento urbano "
                "sustentavel para ampliar oportunidades em Fortaleza.")
        assert detect_language(text) == "pt"

    def test_short_text_returns_none(self):
        assert detect_language("hi") is None

    def test_empty_returns_none(self):
        assert detect_language(None) is None
        assert detect_language("") is None


class TestNavigationDetection:
    """The silent failure the whole project exists to catch: fetch succeeds,
    extractor returns text, and the text is the nav sidebar.

    Caught in production on ustravel.org/node/352363. Thresholds are calibrated
    against the real corpus, not guessed: that nav body scores 0.016 and a
    static "about" page 0.254, while genuine articles run 0.44-1.00.

    Note the sentence counter deliberately skips "U.S." and similar - counting
    ". " naively made this very sample score 0.355 and pass as prose.
    """

    NAV = ("View the Main Menu\n\n\nSearch U.S. Travel Association\n\n\n"
           "Find Members\nAll Stakeholders\nContact Us\nLogin\nRegister\n"
           "About\nEvents\nResearch\nAdvocacy\nMembership\nNewsroom\n"
           "Press Releases\nMedia Contacts\nSubscribe\nFollow Us\n")

    ARTICLE = (
        "The mayor today announced a sweeping overhaul of the city transit network, "
        "promising new bus rapid transit corridors over the coming decade. "
        "Funding will come from a mix of federal grants and a local bond measure that "
        "voters approved last November. Officials said construction could begin as "
        "early as next spring, weather permitting. Critics questioned the timeline "
        "and warned that cost overruns have plagued similar projects elsewhere."
    )

    def test_nav_text_is_detected(self):
        assert looks_like_navigation(self.NAV)

    def test_real_article_is_not(self):
        assert not looks_like_navigation(self.ARTICLE)

    def test_prose_ratio_separates_them(self):
        assert prose_ratio(self.NAV) < 0.30 < prose_ratio(self.ARTICLE)

    def test_empty_counts_as_navigation(self):
        assert looks_like_navigation(None)
        assert looks_like_navigation("")

    def test_nav_body_makes_article_unusable(self):
        """It must not merely warn - a nav body must never reach the sink."""
        # Repeated so the page is long enough for trafilatura to return a body
        # at all. With too little markup it extracts nothing, and the article
        # would then fail for the wrong reason.
        blocks = "".join(
            f"<p>{ln}</p>" for ln in self.NAV.split("\n") if ln.strip()
        ) * 4
        html = page(
            '<script type="application/ld+json">'
            '{"@type":"NewsArticle","headline":"Groups America",'
            '"datePublished":"2026-08-06"}</script>',
            f"<article>{blocks}</article>",
        )
        a = extract_article(html, "https://x.com/node/352363")
        assert a.body_len >= 300, "test setup: trafilatura returned no body"
        assert a.quality["looks_like_navigation"] is True
        assert not a.usable
        assert any("chrome" in w for w in a.warnings)


class TestSitemapLastmodIsNotAPublishDate:
    """`lastmod` means "significantly modified", not "published".

    hccs.edu regenerated its sitemap and five articles from March, April, May
    and August all arrived stamped 24 August - beating the real dates sitting
    in their own OpenGraph tags. The cascade had ranked lastmod top because the
    crawl loop handed it in through `feed_date`, the slot reserved for
    publisher-asserted dates.
    """

    OG = '<html><head><meta property="article:published_time" '          'content="2026-04-21T20:30:00-04:00"></head><body>x</body></html>'
    LASTMOD = "2026-08-24T00:00:00Z"
    URL = "https://hccs.edu/news/2026/april/us-economic-future"

    def test_lastmod_loses_to_the_pages_own_metadata(self):
        r = resolve_date(html=self.OG, url=self.URL, meta={
            "article:published_time": "2026-04-21T20:30:00-04:00"},
            sitemap_lastmod=self.LASTMOD)
        assert r.value.month == 4
        assert r.source is Path.OPENGRAPH

    def test_a_feed_date_still_outranks_the_page(self):
        """The distinction is the point: a feed's pubDate IS publisher-asserted
        and belongs at the top. Only lastmod was in the wrong slot."""
        r = resolve_date(html=self.OG, url=self.URL, meta={
            "article:published_time": "2026-04-21T20:30:00-04:00"},
            feed_raw="2026-04-20T10:00:00Z")
        assert r.source is Path.FEED

    def test_lastmod_is_still_used_when_nothing_better_exists(self):
        """It is a floor, not a lie - evidence a date exists, ranked last."""
        r = resolve_date(html="<html><body>x</body></html>",
                         url="https://x.org/a", sitemap_lastmod=self.LASTMOD)
        assert r.source is Path.SITEMAP
        assert r.value.month == 8

    def test_the_disagreement_is_recorded(self):
        """125 days apart is exactly the drift signal worth surfacing rather
        than silently resolving."""
        r = resolve_date(html=self.OG, url=self.URL, meta={
            "article:published_time": "2026-04-21T20:30:00-04:00"},
            sitemap_lastmod=self.LASTMOD)
        assert r.disagreement_days is not None and r.disagreement_days > 100


def test_the_crawl_loop_routes_dates_by_their_source():
    """Guards the actual wiring: passing a sitemap lastmod through `feed_date`
    is what promoted it to the most trusted signal there is."""
    import inspect

    from scrapev3.crawl import _extract_ref

    src = inspect.getsource(_extract_ref)
    assert 'ref.source in ("rss", "cms_api", "news_sitemap")' in src
    assert "sitemap_lastmod=lastmod" in src
    assert "feed_date=ref.date_raw" not in src


class TestProseRatioSurvivesATrailingList:
    """Real prose followed by a long list is a very common press-release shape.

    Averaging over the whole document punished it: an ACS CAN release of 7,014
    characters of genuine prose scored 0.215 - below the 0.254 a static "about"
    page scores - and was rejected as page chrome, because it ends with its 128
    co-signing organisations, one name per line. 136 lines, median length 31,
    94 of them under 40 characters.

    The question the check should ask is "does a substantial run of this read
    like prose?", not "does all of it?". Chrome has no such run anywhere.
    """

    # Paragraph-per-line, which is the shape trafilatura actually returns - not
    # one unbroken string. The line-length half of the score depends on it.
    PROSE = "\n".join([
        "Today, the American Cancer Society Cancer Action Network, joined by 128 "
        "other patient advocacy groups, submitted a letter responding to the "
        "request for information issued by the Department of Health and Human "
        "Services.",
        "The letter urges the agency to remove financial barriers that keep "
        "patients from taking part in clinical trials. Travel, lodging and lost "
        "wages remain the most frequently cited obstacles for participants who "
        "live far from a trial site.",
        "Signatories asked that reimbursement be treated as a standard component "
        "of trial design rather than an exception granted case by case. The "
        "comment period closes next month.",
        "The coalition also asked the agency to publish participation data by "
        "region, so that gaps in access can be measured rather than inferred "
        "from anecdote. Several members noted that rural patients travel farthest.",
    ]) + "\n"
    # One organisation per line - the shape that sinks the whole-document
    # average. Deliberately free of sentence-ending punctuation, as real
    # signatory lists are: an abbreviation like "Inc." sitting before a
    # capitalised next line reads as a sentence terminator and quietly inflates
    # the score, which is what made the first draft of this fixture fail to
    # reproduce the bug at all.
    SIGNATORIES = "\n".join([
        "Unite for HER", "US Hereditary Angioedema Association",
        "UsAgainstAlzheimer's", "wAIHA Warriors", "Women As One",
        "Young Survival Coalition", "ZERO Prostate Cancer",
        "American Lung Association", "Prevent Cancer Foundation",
        "LUNGevity Foundation", "Triage Cancer", "Fight Colorectal Cancer",
    ] * 12)

    NAV = ("View the Main Menu\nSearch U.S. Travel Association\nFind Members\n"
           "All Stakeholders\nContact Us\nLogin\nRegister\nAbout\nEvents\n"
           "Research\nAdvocacy\nMembership\nNewsroom\nPress Releases\n"
           "Media Contacts\nSubscribe\nFollow Us\n")

    def test_the_trailing_list_drags_the_whole_document_average_down(self):
        """The defect, stated as a measurement rather than an assertion of
        intent - if this ever stops being true the fix is no longer needed."""
        doc = self.PROSE + self.SIGNATORIES
        assert prose_ratio(doc) < 0.30

    def test_but_the_document_is_not_chrome(self):
        doc = self.PROSE + self.SIGNATORIES
        assert best_prose_ratio(doc) > 0.30
        assert not looks_like_navigation(doc)

    def test_a_long_nav_menu_is_still_chrome(self):
        """Windowing must not become a way for page furniture to pass by having
        one good stretch - chrome has no good stretch anywhere."""
        big = self.NAV * 20
        assert len(big) > 1500, "test setup: needs to exceed one window"
        assert best_prose_ratio(big) < 0.30
        assert looks_like_navigation(big)

    def test_prose_buried_after_a_list_is_still_found(self):
        """The window slides; it does not only look at the opening. A page that
        leads with a link list and then carries the article still counts."""
        doc = self.SIGNATORIES + "\n" + self.PROSE * 3
        assert not looks_like_navigation(doc)

    def test_short_bodies_are_unchanged(self):
        """Below one window the measure is the original whole-document one, so
        the calibration on short text is untouched."""
        assert best_prose_ratio(self.NAV) == prose_ratio(self.NAV)
        assert looks_like_navigation(self.NAV)

    def test_empty_is_still_navigation(self):
        assert best_prose_ratio(None) == 0.0
        assert best_prose_ratio("") == 0.0
        assert looks_like_navigation(None)
