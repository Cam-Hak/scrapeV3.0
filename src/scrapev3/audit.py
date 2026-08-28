"""Auditing what discovery actually returns.

The survey measures *capability* - does a feed exist, is there JSON-LD, does
`/wp-json` answer. This measures *correctness*: given what discovery returned,
does it look like this site's articles, or does it look like something else
entirely?

That distinction matters because the worst discovery failures are silent.
`fightcancer.org` returned ten real articles - advocacy actions, events and
legislative summaries from the organisation-wide feed - while its press room
sat untouched. Nothing errored. HTTP 200 throughout. The only way to catch that
class of failure is to ask whether the answer is *plausible*, not whether the
request succeeded.

**The ground truth needs no labelling.** A newsroom page is the publisher's own
statement of what belongs in that section. So the strongest signal available is
simply: do the URLs discovery returned actually appear as links on the page we
were pointed at? Zero overlap across a whole result set means discovery is
looking somewhere else.

Everything here is read-only and fetches no article pages - one request for the
newsroom page plus whatever the discovery cascade spends. Nothing is stored to
the dedup index or to MySQL.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from .discover.sources import discover
from .fetch import PoliteFetcher
from .urls import canonical_url, classify_url, is_non_news_path, registrable_domain

# A result set whose links appear nowhere on the target page is the
# fightcancer.org signature. Below this fraction, say so.
LOW_OVERLAP = 0.20
# Real article sets cluster under one path (/releases/..., /news/...). A set
# spread thinly across many first-segments usually means the site's nav or a
# whole-site sitemap dump.
SCATTERED_BELOW = 0.40
# Above this share of results tripping the non-news veto, discovery is finding
# events and staff pages rather than releases.
NON_NEWS_HEAVY = 0.30
# Below this many links on the newsroom page, there is nothing to corroborate
# against and the overlap number is noise rather than evidence.
MIN_PAGE_LINKS = 20


@dataclass
class Finding:
    """One flag raised against a target, with the number behind it."""

    code: str
    detail: str
    severity: int          # 3 broken, 2 suspicious, 1 worth a look


@dataclass
class TargetAudit:
    a_id: int
    domain: str
    newsroom_url: str

    reachable: bool = False
    status: int = 0
    method: str = "none"
    source_url: str | None = None
    scoped: bool = True

    n_articles: int = 0
    n_page_links: int = 0
    overlap: float | None = None       # None when the page could not be read
    top_prefix: str | None = None
    concentration: float | None = None
    n_non_news: int = 0
    n_off_domain: int = 0
    n_seed_echo: int = 0

    # --- end-to-end probe (only with --extract) -----------------------
    # Discovery finding the right links is the biggest risk, but it is not the
    # whole question. Fetching ONE article per domain costs a single extra
    # request and turns "we found the right URLs" into "we can actually produce
    # a document", which is the number a proposal needs.
    extracted: bool | None = None      # None => not attempted
    extract_status: int = 0
    got_headline: bool = False
    got_body: bool = False
    got_date: bool = False
    body_chars: int = 0
    usable: bool = False
    extract_note: str | None = None

    findings: list[Finding] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Kept apart from errors for the same reason the crawler keeps them apart:
    # a declined site-wide source is the cascade working, and an audit row that
    # files it under "errors" reads as a broken target.
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        """Worst-first ranking. Severity summed, so several small oddities can
        outrank one moderate one - which is usually the right call."""
        return sum(f.severity for f in self.findings)

    @property
    def verdict(self) -> str:
        """Severity 3 is definitive on its own - nothing found, another
        publisher's content, nothing matching the page. Those are not "a bit
        odd", so they are not allowed to be outvoted by the absence of milder
        flags. Below that, small oddities accumulate."""
        if any(f.severity >= 3 for f in self.findings):
            return "broken"
        if self.score >= 2:
            return "suspicious"
        if self.score >= 1:
            return "check"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "findings"}
        d["findings"] = [f.__dict__ for f in self.findings]
        d["score"] = self.score
        d["verdict"] = self.verdict
        return d


def page_links(html: str, page_url: str, base_url: str | None = None) -> set[str]:
    """Every same-site link on the page, canonicalised.

    Deliberately *not* filtered to article-shaped links: this is the
    corroboration set, and a discovered URL appearing anywhere on the page -
    even in a sidebar - is evidence discovery is looking in the right place.
    """
    out: set[str] = set()
    domain = registrable_domain(page_url)
    # Relative hrefs resolve against where the response came from, not against
    # the canonicalised URL we asked for. On a site that redirects (and whose
    # canonical form has lost its trailing slash) the two resolve differently,
    # and the corroboration set ends up full of URLs that exist nowhere - which
    # reads downstream as `no_overlap`, i.e. discovery blamed for a join bug.
    base = base_url or page_url
    for node in LexborHTMLParser(html).css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = canonical_url(urljoin(base, href))
        if absolute and registrable_domain(absolute) == domain:
            out.add(absolute)
    return out


def _parent_path(url: str) -> str:
    """The directory an article sits in.

    NOT the first path segment. Plenty of sites put articles at the root -
    nasda.org publishes `/nasda-and-nalc-roll-out-new-data` - so the first
    segment is unique per article and concentration reads 4% on a perfect
    result set. The directory is `/` for all of them, which is the clustering
    the measure was after.
    """
    path = urlsplit(url).path.rstrip("/")
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def judge(a: TargetAudit) -> None:
    """Turn the measurements into named findings. Every rule states its number,
    so a flag can be argued with rather than just believed."""
    if not a.reachable:
        a.findings.append(Finding("unreachable", f"newsroom page returned {a.status}", 3))
        return

    if a.n_articles == 0:
        a.findings.append(Finding("no_articles", "discovery returned nothing", 3))
        return

    # Overlap is only meaningful when the page had links to corroborate
    # against. A JS-rendered newsroom whose article list loads after paint
    # gives a near-empty page, and calling that "discovery is broken" blames
    # the wrong component.
    if a.overlap is not None and a.n_page_links < MIN_PAGE_LINKS:
        a.findings.append(Finding(
            "page_too_thin",
            f"newsroom page exposed only {a.n_page_links} link(s), so overlap "
            f"({a.overlap:.0%}) proves nothing - likely rendered by JavaScript", 1))
    elif a.overlap is not None:
        if a.overlap == 0.0:
            a.findings.append(Finding(
                "no_overlap",
                f"none of {a.n_articles} results are linked from the newsroom page", 3))
        elif a.overlap < LOW_OVERLAP:
            a.findings.append(Finding(
                "low_overlap",
                f"only {a.overlap:.0%} of results are linked from the newsroom page", 2))

    if a.concentration is not None and a.concentration < SCATTERED_BELOW:
        a.findings.append(Finding(
            "scattered",
            f"results spread across many paths; biggest is {a.top_prefix} "
            f"at {a.concentration:.0%}", 2))

    if a.n_articles and a.n_non_news / a.n_articles > NON_NEWS_HEAVY:
        a.findings.append(Finding(
            "non_news_heavy",
            f"{a.n_non_news}/{a.n_articles} results look like events or staff pages", 2))

    if a.n_off_domain:
        a.findings.append(Finding(
            "off_domain",
            f"{a.n_off_domain} result(s) belong to another publisher", 3))

    if a.n_seed_echo:
        a.findings.append(Finding(
            "seed_echo", "discovery returned the newsroom page itself", 1))

    if not a.scoped:
        a.findings.append(Finding(
            "unscoped", "source could not be narrowed to the target's section", 1))

    if a.method == "listing":
        a.findings.append(Finding(
            "last_resort", "only the listing page worked - no feed, no usable sitemap", 1))

    # Extraction, when it was probed. A domain whose discovery is perfect and
    # whose articles will not extract is just as unusable, and for a proposal
    # the two failures have to be counted separately because they have
    # completely different fixes.
    if a.extracted is False:
        a.findings.append(Finding(
            "article_unfetchable",
            f"the first article returned {a.extract_status or 'no response'}", 3))
    elif a.extracted and not a.usable:
        missing = [n for n, ok in (("headline", a.got_headline),
                                   ("body", a.got_body), ("date", a.got_date)) if not ok]
        detail = (f"missing {', '.join(missing)}" if missing
                  else f"body rejected as page chrome ({a.body_chars} chars)")
        a.findings.append(Finding("extract_failed", detail, 3))


async def audit_target(fetcher: PoliteFetcher, *, a_id: int, domain: str,
                       newsroom_url: str, known_method: str | None = None,
                       known_feed: str | None = None, feed_absent: bool = False,
                       limit: int = 25, extract: bool = False) -> TargetAudit:
    """Run discovery against one target and score what came back."""
    a = TargetAudit(a_id=a_id, domain=domain, newsroom_url=newsroom_url)

    resp = await fetcher.get(newsroom_url)
    a.status = resp.status
    a.reachable = bool(resp.ok)
    links: set[str] = set()
    if resp.ok:
        try:
            links = page_links(resp.text, newsroom_url,
                               base_url=resp.final_url or newsroom_url)
        except Exception as exc:                            # noqa: BLE001
            a.errors.append(f"page parse failed: {type(exc).__name__}")
    elif resp.wall:
        a.errors.append(f"bot wall: {resp.wall}")
    a.n_page_links = len(links)

    if not a.reachable:
        judge(a)
        return a

    try:
        found = await discover(fetcher, newsroom_url, known_feed=known_feed,
                               known_method=known_method, feed_absent=feed_absent,
                               limit=limit)
    except Exception as exc:                                # noqa: BLE001
        a.errors.append(f"discovery raised {type(exc).__name__}: {exc}")
        judge(a)
        return a

    a.method = found.method
    a.source_url = found.feed_url
    a.scoped = found.scoped
    a.errors.extend(found.errors)
    a.notes.extend(found.notes)

    urls = [canonical_url(r.url) for r in found.articles]
    a.n_articles = len(urls)
    a.sample = urls[:5]
    if not urls:
        judge(a)
        return a

    seed = canonical_url(newsroom_url)
    a.n_seed_echo = sum(1 for u in urls if u == seed)
    a.n_off_domain = sum(1 for u in urls if registrable_domain(u) != domain)
    a.n_non_news = sum(1 for u in urls if is_non_news_path(u))

    if links:
        a.overlap = sum(1 for u in urls if u in links) / len(urls)

    counts = collections.Counter(_parent_path(u) for u in urls)
    a.top_prefix, top_n = counts.most_common(1)[0]
    a.concentration = top_n / len(urls)

    if extract and urls:
        await _probe_extraction(fetcher, a, urls, a.method)

    judge(a)
    return a


def _first_crawlable(urls: list[str], method: str) -> str | None:
    """The first result the real crawl loop would actually fetch.

    An audit that probes a URL the crawler would have skipped is measuring
    something that never happens. `crawl_target` puts sitemap- and
    listing-sourced URLs through `classify_url`, because those sources contain
    section indexes, search pages and pagination; feed and CMS-API URLs are
    trusted because those sources only publish articles.

    Skipping that gate here made extraction look far worse than it is: the
    audit was fetching `sanjac.edu/about/news/index.php` and
    `aarcorp.com/en/newsroom/search`, both of which the crawler rejects without
    spending a request.
    """
    for u in urls:
        if is_non_news_path(u):
            continue
        if method in ("sitemap", "listing") and not classify_url(u).is_article:
            continue
        return u
    return None


async def _probe_extraction(fetcher: PoliteFetcher, a: TargetAudit,
                            urls: list[str], method: str) -> None:
    """Fetch the first discovered article and see whether it extracts.

    One article, not all of them: the question is whether this domain's pages
    yield a document at all, and a single sample answers it for one extra
    request. Anything more turns a corpus audit into a crawl.
    """
    from .extract import extract_article

    target = _first_crawlable(urls, method)
    if target is None:
        a.extracted = False
        a.extract_note = "no result survives the crawl's own URL gates"
        return
    try:
        resp = await fetcher.get(target)
    except Exception as exc:                                # noqa: BLE001
        a.extracted = False
        a.extract_note = f"{type(exc).__name__}: {exc}"
        return
    a.extract_status = resp.status
    if not resp.ok:
        a.extracted = False
        a.extract_note = resp.wall or resp.error or f"HTTP {resp.status}"
        return

    art = extract_article(resp.text, target)
    a.extracted = True
    a.got_headline = bool(art.headline)
    a.got_body = art.body_len >= 300
    a.got_date = art.date.value is not None
    a.body_chars = art.body_len
    a.usable = art.usable
    if art.warnings:
        a.extract_note = art.warnings[0][:120]


def summarize(results: list[TargetAudit]) -> dict[str, Any]:
    n = len(results) or 1
    verdicts = collections.Counter(r.verdict for r in results)
    methods = collections.Counter(r.method for r in results)
    codes = collections.Counter(f.code for r in results for f in r.findings)
    with_overlap = [r.overlap for r in results if r.overlap is not None]
    return {
        "targets": len(results),
        "verdicts": dict(verdicts),
        "methods": dict(methods),
        "findings": dict(codes),
        "median_overlap": (sorted(with_overlap)[len(with_overlap) // 2]
                           if with_overlap else None),
        "zero_yield": sum(1 for r in results if r.reachable and r.n_articles == 0),
        "unreachable": sum(1 for r in results if not r.reachable),
        "pct_clean": round(100 * verdicts.get("ok", 0) / n, 1),
        "by_tld": _by_tld(results),
        "extraction": _extraction_summary(results),
    }


def _by_tld(results: list[TargetAudit]) -> dict[str, Any]:
    """The corpus is .edu/.gov/.org institutional, and those behave very
    differently - a proposal needs the cut, not just the total."""
    out: dict[str, dict[str, int]] = {}
    for r in results:
        tld = "." + r.domain.rsplit(".", 1)[-1] if "." in r.domain else "?"
        bucket = out.setdefault(tld, {"targets": 0, "ok": 0, "workable": 0})
        bucket["targets"] += 1
        if r.verdict == "ok":
            bucket["ok"] += 1
        if r.verdict in ("ok", "check"):
            bucket["workable"] += 1
    for b in out.values():
        b["pct_workable"] = round(100 * b["workable"] / b["targets"], 1)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["targets"]))


def _extraction_summary(results: list[TargetAudit]) -> dict[str, Any]:
    probed = [r for r in results if r.extracted is not None]
    if not probed:
        return {"probed": 0}
    fetched = [r for r in probed if r.extracted]
    return {
        "probed": len(probed),
        "article_fetched": len(fetched),
        "pct_fetched": round(100 * len(fetched) / len(probed), 1),
        "got_headline": sum(1 for r in fetched if r.got_headline),
        "got_body": sum(1 for r in fetched if r.got_body),
        "got_date": sum(1 for r in fetched if r.got_date),
        "usable": sum(1 for r in fetched if r.usable),
        "pct_usable_of_probed": round(100 * sum(1 for r in fetched if r.usable)
                                      / len(probed), 1),
    }
