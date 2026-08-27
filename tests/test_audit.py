"""The discovery audit.

Discovery's worst failures are silent: it returns real articles, just the wrong
ones, with no error and HTTP 200 throughout. `fightcancer.org` collected ten
advocacy actions while its press room sat untouched.

So these tests pin the judgement, not the plumbing - each rule has to fire on
the shape it was written for and stay quiet on a healthy site, because a scorer
that flags everything is exactly as useless as one that flags nothing.
"""

from __future__ import annotations

from scrapev3.audit import (LOW_OVERLAP, NON_NEWS_HEAVY, SCATTERED_BELOW,
                            TargetAudit, judge, page_links, summarize)

PAGE = "https://www.fightcancer.org/press-room/search"


def healthy(**kw) -> TargetAudit:
    """A target where discovery did the right thing. Every test below starts
    from this and breaks one thing."""
    a = TargetAudit(a_id=1, domain="fightcancer.org", newsroom_url=PAGE,
                    reachable=True, status=200, method="rss", n_articles=10,
                    n_page_links=40, overlap=1.0, top_prefix="/releases",
                    concentration=1.0)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def codes(a: TargetAudit) -> set[str]:
    judge(a)
    return {f.code for f in a.findings}


# ---------------------------------------------------------------------------
# the healthy case must stay quiet
# ---------------------------------------------------------------------------

def test_a_good_target_raises_nothing():
    a = healthy()
    assert codes(a) == set()
    assert a.verdict == "ok"
    assert a.score == 0


# ---------------------------------------------------------------------------
# the failure that motivated the whole thing
# ---------------------------------------------------------------------------

def test_results_linked_nowhere_on_the_page_are_broken():
    """The fightcancer.org signature: ten real articles, none of them from this
    section. The newsroom page is the publisher's own statement of what belongs
    there, so zero overlap means discovery looked somewhere else."""
    a = healthy(overlap=0.0)
    assert "no_overlap" in codes(a)
    assert a.verdict == "broken"


def test_thin_overlap_is_suspicious_not_broken():
    """A feed legitimately runs ahead of a cached listing page, so a couple of
    unmatched items is normal. A large majority unmatched is not."""
    a = healthy(overlap=LOW_OVERLAP / 2)
    assert "low_overlap" in codes(a)
    assert a.verdict == "suspicious"


def test_overlap_just_above_the_floor_is_accepted():
    assert "low_overlap" not in codes(healthy(overlap=LOW_OVERLAP + 0.01))


def test_a_page_we_could_not_read_is_not_held_against_the_target():
    """No overlap measurement is different from an overlap of zero, and
    conflating them would flag every bot-walled site as broken discovery."""
    assert codes(healthy(overlap=None)) == set()


# ---------------------------------------------------------------------------
# whole-site dumps
# ---------------------------------------------------------------------------

def test_results_scattered_across_paths_are_flagged():
    """Real article sets cluster: /releases/..., /news/... . A set spread
    thinly across first-segments is the shape of a nav harvest or a sitemap
    that could not be narrowed to the section."""
    a = healthy(concentration=SCATTERED_BELOW / 2, top_prefix="/what-we-do")
    assert "scattered" in codes(a)


def test_a_tightly_clustered_set_is_not_flagged():
    assert "scattered" not in codes(healthy(concentration=0.9))


def test_a_source_that_could_not_be_scoped_is_noted():
    a = healthy(scoped=False)
    assert "unscoped" in codes(a)
    assert a.verdict == "check"          # a hint, not a condemnation


# ---------------------------------------------------------------------------
# wrong kind of content
# ---------------------------------------------------------------------------

def test_mostly_events_and_staff_pages_is_flagged():
    a = healthy(n_articles=10, n_non_news=int(10 * NON_NEWS_HEAVY) + 1)
    assert "non_news_heavy" in codes(a)


def test_a_few_non_news_results_are_tolerated():
    assert "non_news_heavy" not in codes(healthy(n_articles=10, n_non_news=1))


def test_another_publishers_content_is_always_broken():
    """ufw.org's press-clipping feed linked to Politico and WBUR, and those got
    stored under UFW's agency id. Attributing one publisher's article to
    another is an integrity problem, not a cosmetic one - so a single hit is
    enough."""
    a = healthy(n_off_domain=1)
    assert "off_domain" in codes(a)
    assert a.verdict == "broken"


def test_returning_the_newsroom_page_itself_is_noted():
    """battelle.org's own newsroom URL was once stored as an article, with the
    site's nav text as its body."""
    assert "seed_echo" in codes(healthy(n_seed_echo=1))


# ---------------------------------------------------------------------------
# outright failures short-circuit
# ---------------------------------------------------------------------------

def test_an_unreachable_target_reports_that_and_stops():
    """No point scoring discovery on a page we never read - the one useful fact
    is the status code."""
    a = TargetAudit(a_id=1, domain="x.org", newsroom_url=PAGE,
                    reachable=False, status=403)
    assert codes(a) == {"unreachable"}
    assert "403" in a.findings[0].detail


def test_zero_results_reports_that_and_stops():
    a = healthy(n_articles=0)
    assert codes(a) == {"no_articles"}


def test_the_listing_page_winning_is_worth_knowing_but_not_alarming():
    """It works - it is just the last rung, so it has no publisher-asserted
    headline or date behind it and is the most fragile to a redesign."""
    a = healthy(method="listing")
    assert "last_resort" in codes(a)
    assert a.verdict == "check"


def test_flags_accumulate_into_a_worse_verdict():
    """Several small oddities together usually do mean something, so severity
    sums rather than taking the maximum."""
    a = healthy(method="listing", scoped=False, n_seed_echo=1)
    judge(a)
    assert a.score == 3
    assert a.verdict == "suspicious"


# ---------------------------------------------------------------------------
# the corroboration set
# ---------------------------------------------------------------------------

HTML = """
<html><body>
  <nav><a href="/what-we-do/access">Access</a></nav>
  <main>
    <a href="/releases/one">One</a>
    <a href="https://www.fightcancer.org/releases/two">Two</a>
    <a href="https://twitter.com/acscan">Twitter</a>
    <a href="#skip">Skip</a>
    <a href="mailto:x@y.org">Mail</a>
  </main>
</body></html>
"""


def test_page_links_collects_same_site_links_canonicalised():
    links = page_links(HTML, PAGE)
    assert "https://fightcancer.org/releases/one" in links
    assert "https://fightcancer.org/releases/two" in links


def test_page_links_excludes_other_sites_and_non_pages():
    links = page_links(HTML, PAGE)
    assert not any("twitter.com" in u for u in links)
    assert not any(u.endswith("#skip") for u in links)
    assert not any(u.startswith("mailto:") for u in links)


def test_page_links_keeps_nav_links_on_purpose():
    """This set is for corroboration, not harvesting. A discovered URL showing
    up anywhere on the page - even the sidebar - is evidence discovery is
    looking in the right place, so filtering to article-shaped links here would
    throw away signal."""
    assert "https://fightcancer.org/what-we-do/access" in page_links(HTML, PAGE)


# ---------------------------------------------------------------------------
# the roll-up
# ---------------------------------------------------------------------------

def test_summary_counts_verdicts_methods_and_flags():
    good = healthy()
    bad = healthy(overlap=0.0, method="sitemap")
    for a in (good, bad):
        judge(a)
    s = summarize([good, bad])
    assert s["targets"] == 2
    assert s["verdicts"] == {"ok": 1, "broken": 1}
    assert s["methods"] == {"rss": 1, "sitemap": 1}
    assert s["findings"]["no_overlap"] == 1
    assert s["pct_clean"] == 50.0


def test_summary_survives_an_empty_run():
    s = summarize([])
    assert s["targets"] == 0
    assert s["pct_clean"] == 0.0
