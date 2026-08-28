"""Turning a newsroom URL into article URLs.

Four sources, ordered by what the Phase 1 survey actually measured rather than
by what the literature suggests:

    1. CMS JSON API  - WordPress on 37.4% of domains, 82.4% of those with a
                       usable /wp-json. Returns headline, FULL body HTML and
                       exact date_gmt in ONE request. Highest yield by far.
    2. RSS / Atom    - 51.6% of domains (34.5% autodiscovered, 17.1% only found
                       by path probing). 23.1% carry content:encoded, i.e. the
                       whole body, eliminating the article fetch entirely.
    3. Sitemap       - 82.2% coverage but weakest signal: mostly just URLs and
                       a lastmod that means "significantly modified", not
                       "published".
    4. Listing page  - last resort, 9.0% of domains. Harvest links and classify.

Anything a source supplies directly (headline, date, body) is passed through to
the extractor, which treats publisher-asserted values as authoritative. That is
why the cheap sources are also the accurate ones.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..fetch import PoliteFetcher
from ..urls import canonical_url, classify_url, registrable_domain

FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml",
              "/feed/", "/news/feed", "/blog/feed")
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
                 "/sitemap-index.xml", "/news-sitemap.xml")
WP_PATHS = ("/wp-json/wp/v2/posts", "/?rest_route=/wp/v2/posts")

_TAG_RE = re.compile(r"<[^>]+>")

# A source has to be mostly THIS publisher's content to be the right source.
# Below this share it is someone else's newsroom, however real the articles are.
MIN_OWN_CONTENT = 0.5
# Small result sets are judged on content, not ratio - one syndicated link in a
# three-item feed says nothing.
MIN_RESULTS_FOR_RATIO = 5
# Below this many links on the target page there is nothing to corroborate a
# site-wide source against, so it gets the benefit of the doubt.
MIN_PAGE_LINKS_TO_JUDGE = 20


@dataclass
class ArticleRef:
    """A discovered article, with whatever the source could supply for free."""

    url: str
    headline: str | None = None
    date_raw: str | None = None
    body_html: str | None = None      # feeds/CMS sometimes give the whole body
    source: str = "listing"

    @property
    def has_full_body(self) -> bool:
        return bool(self.body_html and len(_TAG_RE.sub("", self.body_html)) > 500)


@dataclass
class Discovery:
    method: str = "none"
    # The URL that actually worked - a feed, a sitemap, or a CMS endpoint.
    # Stored on the target so the next run goes straight back to it instead of
    # re-probing the whole cascade. Named `feed_url` because that is the column
    # it persists to; it is not always a feed.
    feed_url: str | None = None
    articles: list[ArticleRef] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Separate from `errors` because they are not failures: they are the
    # cascade correctly declining a source that answers for the whole site
    # while the target is one section of it. Filing them as errors made every
    # healthy sectioned target report errors, which is how a run with nothing
    # wrong ends up printing nine of them - and how an error list stops being
    # read at all.
    notes: list[str] = field(default_factory=list)
    # True when every feed path was probed and none existed. Cached so we stop
    # paying for the same nine 404s on every daily run.
    feed_absent: bool = False
    # False when a section was asked for and this source could not match it, so
    # the result is the whole site rather than the target's own content. Such a
    # result is still usable, but it must not outrank the target's own page.
    scoped: bool = True


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _xml_root(text: str):
    """Parse XML defensively - feeds in the wild are frequently malformed."""
    try:
        from lxml import etree

        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
        return etree.fromstring(text.encode("utf-8", "replace"), parser=parser)
    except Exception:
        return None


def _localname(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


# ---------------------------------------------------------------------------
# 1. WordPress / CMS JSON API - highest yield
# ---------------------------------------------------------------------------

async def from_wp_json(fetcher: PoliteFetcher, base: str, limit: int = 50) -> Discovery:
    """Query the WordPress REST API.

    `_fields` keeps the response small, and `orderby=date` gives newest first
    so the caller can stop early once it reaches already-seen articles.
    """
    out = Discovery(method="cms_api")
    fields = "id,link,date_gmt,modified_gmt,title,content,excerpt"
    for path in WP_PATHS:
        sep = "&" if "?" in path else "?"
        url = f"{base}{path}{sep}per_page={min(limit, 100)}&orderby=date&order=desc&_fields={fields}"
        resp = await fetcher.get(url)
        if not resp.ok:
            continue
        text = resp.text.lstrip()
        if not text.startswith("["):
            # A dict here is usually {"code":"rest_cannot_access",...}
            continue
        try:
            posts = json.loads(text)
        except Exception:
            continue
        if not isinstance(posts, list):
            continue

        for post in posts:
            if not isinstance(post, dict):
                continue
            link = post.get("link")
            if not link:
                continue
            title = (post.get("title") or {}).get("rendered")
            content = (post.get("content") or {}).get("rendered")
            out.articles.append(ArticleRef(
                url=canonical_url(link),
                headline=_unescape(title) if title else None,
                # date_gmt is exact and publisher-asserted - far better than
                # anything we could infer from the page.
                date_raw=post.get("date_gmt"),
                body_html=content,
                source="cms_api",
            ))
        if out.articles:
            out.feed_url = url
            return out

    out.method = "none"
    out.errors.append("no usable wp-json endpoint")
    return out


def _unescape(text: str) -> str:
    import html as _html

    return _html.unescape(_TAG_RE.sub("", text)).strip()


# ---------------------------------------------------------------------------
# 2. RSS / Atom
# ---------------------------------------------------------------------------

def parse_feed(text: str, base_url: str) -> list[ArticleRef]:
    root = _xml_root(text)
    if root is None:
        return []

    refs: list[ArticleRef] = []
    for node in root.iter():
        name = _localname(node.tag)
        if name not in ("item", "entry"):
            continue

        url = headline = date_raw = body = None
        for child in node:
            cname = _localname(child.tag)
            if cname == "link":
                # RSS puts the URL in the text; Atom puts it in @href.
                url = url or (child.get("href") or (child.text or "").strip())
            elif cname == "title" and child.text:
                headline = headline or child.text.strip()
            elif cname in ("pubdate", "published", "date", "updated", "created"):
                date_raw = date_raw or (child.text or "").strip()
            elif cname == "encoded" and child.text:
                # content:encoded - the full article body, 23.1% of feeds.
                body = child.text
            elif cname == "content" and child.text and not body:
                body = child.text
            elif cname == "description" and child.text and not body:
                body = child.text
            elif cname == "guid" and not url and child.text:
                candidate = child.text.strip()
                if candidate.startswith("http"):
                    url = candidate

        if not url:
            continue
        refs.append(ArticleRef(
            url=canonical_url(urljoin(base_url, url)),
            headline=headline,
            date_raw=date_raw,
            body_html=body,
            source="rss",
        ))
    return refs


async def find_feed(
    fetcher: PoliteFetcher,
    page_url: str,
    html: str | None = None,
    *,
    skip_probe: bool = False,
    base_url: str | None = None,
) -> tuple[str | None, bool, bool]:
    """Find a feed. Returns (feed_url, probed_and_found_nothing, autodiscovered).

    `autodiscovered` matters downstream: a feed the target page declares is
    authoritative for that page, while one found by probing the site root is
    only a guess that the root feed covers this section.

    Autodiscovery first, then path probing. Probing is not optional - it found
    17.1% of domains' feeds versus 34.5% for autodiscovery, so skipping it
    loses a third of all feed coverage.

    But probing is *expensive*: nine paths at the per-host delay is ~45 seconds
    to prove a feed does not exist, and battelle.org paid exactly that on every
    run. `skip_probe` lets the caller honour a cached "no feed here" verdict.
    Autodiscovery still runs, since it is free once the page is in hand and a
    site may have added a feed since we last looked.
    """
    if html:
        tree = LexborHTMLParser(html)
        for node in tree.css('link[rel="alternate"]'):
            ctype = (node.attributes.get("type") or "").lower()
            href = node.attributes.get("href")
            if href and ("rss" in ctype or "atom" in ctype or "xml" in ctype):
                return canonical_url(urljoin(base_url or page_url, href)), False, True

    if skip_probe:
        return None, False, False   # cached absence - do not re-probe, do not re-cache

    base = _origin(page_url)
    for path in FEED_PATHS:
        resp = await fetcher.get(base + path)
        if resp.ok and ("<rss" in resp.text[:2000] or "<feed" in resp.text[:2000]):
            return canonical_url(base + path), False, False
    return None, True, False        # probed everything, found nothing


async def from_feed(fetcher: PoliteFetcher, feed_url: str) -> Discovery:
    out = Discovery(method="rss", feed_url=feed_url)
    resp = await fetcher.get(feed_url)
    if not resp.ok:
        out.method = "none"
        out.errors.append(f"feed fetch failed: {resp.error or resp.status}")
        return out
    out.articles = parse_feed(resp.text, resp.final_url or feed_url)
    if not out.articles:
        out.method = "none"
        out.errors.append("feed parsed but contained no items")
    return out


# ---------------------------------------------------------------------------
# 3. Sitemap
# ---------------------------------------------------------------------------

def parse_sitemap(text: str) -> tuple[list[str], list[ArticleRef]]:
    """Return (child sitemap URLs, article refs)."""
    root = _xml_root(text)
    if root is None:
        return [], []

    children: list[str] = []
    refs: list[ArticleRef] = []

    for node in root.iter():
        name = _localname(node.tag)
        if name == "sitemap":
            for child in node:
                if _localname(child.tag) == "loc" and child.text:
                    children.append(child.text.strip())
        elif name == "url":
            loc = lastmod = title = pub_date = None
            for child in node:
                cname = _localname(child.tag)
                if cname == "loc" and child.text:
                    loc = child.text.strip()
                elif cname == "lastmod" and child.text:
                    lastmod = child.text.strip()
                elif cname == "news":
                    # Google News extension: publisher-asserted title and date.
                    for gc in child.iter():
                        gname = _localname(gc.tag)
                        if gname == "title" and gc.text:
                            title = gc.text.strip()
                        elif gname == "publication_date" and gc.text:
                            pub_date = gc.text.strip()
            if loc:
                refs.append(ArticleRef(
                    url=canonical_url(loc),
                    headline=title,
                    # Prefer the news publication_date; lastmod only means
                    # "significantly modified" and drifts on template changes.
                    date_raw=pub_date or lastmod,
                    source="news_sitemap" if pub_date else "sitemap",
                ))
    return children, refs


async def from_sitemap(
    fetcher: PoliteFetcher,
    base: str,
    limit: int = 100,
    *,
    known_sitemap: str | None = None,
    section: str | None = None,
) -> Discovery:
    """Walk sitemaps for article URLs.

    `known_sitemap` short-circuits the whole index walk when a previous run
    already found the leaf that works. `section` is the target's own path (e.g.
    "/insights/newsroom") and biases both which child sitemaps are opened and
    which URLs are kept - battelle.org's index otherwise hands back conference
    proceedings and event pages that the crawl gates then discard, which is
    correct but wasteful.
    """
    out = Discovery(method="sitemap")
    if known_sitemap:
        queue = [known_sitemap]
    else:
        rules = await fetcher.robots_for(base + "/")
        queue = (list(rules.sitemaps) or [base + p for p in SITEMAP_PATHS])[:5]

    seen: set[str] = set()
    depth = 0
    winner: str | None = None

    while queue and depth < 2 and len(out.articles) < limit:
        depth += 1
        next_queue: list[str] = []
        for sm_url in queue[:5]:
            if sm_url in seen:
                continue
            seen.add(sm_url)
            resp = await fetcher.get(sm_url)
            if not resp.ok:
                continue
            children, refs = parse_sitemap(resp.text)
            if section:
                in_section = [r for r in refs
                              if urlsplit(r.url).path.lower().startswith(section)]
                # Only narrow when the section actually matches something -
                # plenty of sites publish articles outside their listing path.
                # But record that we could not, because an unscoped sitemap is
                # a dump of the entire site: fightcancer.org's yields
                # /what-we-do/... and /policy-resources/... , every one of them
                # a real page and none of them a press release.
                if in_section:
                    refs = in_section
                else:
                    out.scoped = False
            if refs:
                if winner is None:
                    winner = sm_url
                out.articles.extend(refs)
                if any(r.source == "news_sitemap" for r in refs):
                    out.method = "news_sitemap"
            ranked = sorted(children, key=lambda u: _sitemap_priority(u, section))
            next_queue.extend(ranked[:3])
        queue = next_queue

    if not out.articles:
        out.method = "none"
        out.errors.append("no sitemap urls found")
    else:
        # Newest first, THEN truncate. A sitemap carries no ordering guarantee
        # and plenty are oldest-first: crnusa.org's opens with 2016 and its
        # newest entry is nine years later. Taking the first N in document
        # order fetched twenty-five 2016 articles and then rejected every one
        # of them for being outside the date window - paying for the fetch to
        # learn what `lastmod` already said.
        out.articles.sort(key=_recency_key, reverse=True)
        out.articles = out.articles[:limit]
        out.feed_url = winner          # remember the leaf that worked
    return out


# `lastmod` and Google News `publication_date` are ISO 8601, which sorts
# correctly as plain text - so ordering thousands of refs costs no date
# parsing at all. Anything that is not ISO sorts last rather than guessing.
_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _recency_key(ref: ArticleRef) -> str:
    raw = (ref.date_raw or "").strip()
    return raw if _ISO_PREFIX.match(raw) else ""


def _sitemap_priority(url: str, section: str | None = None) -> int:
    """Rank child sitemaps: most likely to hold this target's articles first."""
    low = url.lower()
    if section and section.strip("/") and section.strip("/").split("/")[0] in low:
        return 0
    if "news" in low:
        return 1
    if "post" in low or "article" in low or "press" in low:
        return 2
    if re.search(r"20\d{2}", low):
        return 3
    return 4


# ---------------------------------------------------------------------------
# 4. Listing page - last resort
# ---------------------------------------------------------------------------

# Landmark elements that hold site chrome by definition. Structural, not a list
# of class names - guessing at classes is the per-site knowledge this project
# exists to eliminate, and it is what rots.
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})


def _in_chrome(node) -> bool:
    """Is this link inside a navigation landmark rather than page content?"""
    parent = node.parent
    while parent is not None:
        if parent.tag in _CHROME_TAGS:
            return True
        if (parent.attributes.get("role") or "").lower() in ("navigation", "banner",
                                                             "contentinfo"):
            return True
        parent = parent.parent
    return False


# CAM: gathering all the anchor href links from a page and determining which ones are
# the important ones
def harvest_links(html: str, page_url: str, limit: int = 60,
                  *, section: str | None = None,
                  base_url: str | None = None) -> list[ArticleRef]:
    """Score every same-domain link and keep the article-shaped ones.

    `base_url` is the URL the response actually came from, and relative hrefs
    resolve against it - never against `page_url`, which is the canonicalised
    URL we asked for. The two differ whenever a site redirects, and the
    difference is not cosmetic: ccu.edu/news redirects to www.ccu.edu/news/,
    and its links are relative with no leading slash ("2026/some-story/").
    Resolving those against the slash-less canonical form makes urljoin treat
    "news" as a file rather than a directory, so every article URL lost its
    /news/ segment and 404ed - twelve fetches, twelve failures, no articles.

    A listing page links to everything on the site, so "same domain and
    article-shaped" is far too permissive on its own - battelle.org's
    press-release page yielded /markets/national-security/... marketing pages
    that pass the URL classifier comfortably. Two filters narrow it, applied in
    order of how much they can be trusted:

    1. **The target's own section.** Proven, and used when it matches anything.

    2. **Not inside a navigation landmark.** The fallback, for the very common
       CMS layout where the listing and its articles live on different paths.
       fightcancer.org lists press releases at /press-room/search while the
       releases themselves are at /releases/<slug>, so section scoping finds
       nothing at all there. On that page, 43 links pass the URL classifier;
       the 10 real releases are the only ones *not* inside a nav landmark,
       while all 33 others are. Note that the classifier scores every one of
       them 2.00 - the URL shape genuinely cannot tell them apart, and only the
       document structure can.
    """
    tree = LexborHTMLParser(html)
    domain = registrable_domain(page_url)
    base = base_url or page_url
    # (score, url, in_chrome)
    scored: list[tuple[float, str, bool]] = []
    seen: set[str] = set()

    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = canonical_url(urljoin(base, href))
        if not absolute or absolute in seen or absolute == canonical_url(page_url):
            continue
        if registrable_domain(absolute) != domain:
            continue
        seen.add(absolute)
        verdict = classify_url(absolute)
        if verdict.is_article:
            scored.append((verdict.score, absolute, _in_chrome(node)))

    in_section = [s for s in scored
                  if section and urlsplit(s[1]).path.lower().startswith(section)]
    if in_section:
        kept = in_section
    else:
        # Never widen to "everything on the page" - that is what re-harvests the
        # nav. Widen only to what is outside the navigation landmarks, and if
        # there is nothing there, the honest answer is still zero.
        kept = [s for s in scored if not s[2]]

    kept.sort(reverse=True)
    return [ArticleRef(url=u, source="listing") for _, u, _ in kept[:limit]]


async def from_listing(fetcher: PoliteFetcher, page_url: str, html: str | None = None,
                       limit: int = 60, *, section: str | None = None,
                       base_url: str | None = None) -> Discovery:
    out = Discovery(method="listing")
    if html is None:
        resp = await fetcher.get(page_url)
        if not resp.ok:
            out.method = "none"
            out.errors.append(f"listing fetch failed: {resp.error or resp.status}")
            return out
        html = resp.text
        # Where the HTML actually came from, which is what relative links
        # resolve against. A caller supplying `html` must supply this too.
        base_url = resp.final_url or page_url
    out.articles = harvest_links(html, page_url, limit=limit, section=section,
                                 base_url=base_url)
    if not out.articles and section:
        # Nothing under the target's own section. Widening to the whole site
        # would just harvest the global nav, so report the honest answer.
        out.errors.append(f"no article links under {section}")
    if not out.articles:
        out.method = "none"
        out.errors.append("no article-shaped links on listing page")
    return out


# ---------------------------------------------------------------------------
# The cascade
# ---------------------------------------------------------------------------

def _root_feed_for_section(feed_url: str, section: str | None) -> bool:
    """A site-root feed paired with a target that is one section of the site.

    The shape that cannot be trusted without corroboration - and a free check,
    since it reads the URL rather than fetching anything. It matters on the
    fast path especially: a target that once cached the wrong feed would
    otherwise keep going straight back to it on every run, with the corrected
    cascade never getting a look in.

    A feed on a deeper path (/press-room/rss.xml) is section-specific and needs
    no corroboration.
    """
    if not section:
        return False
    return len([p for p in urlsplit(feed_url).path.split("/") if p]) <= 1


def _covers_target(found: Discovery, listing_html: str | None,
                        newsroom_url: str, section: str | None,
                        declared: bool, base_url: str | None = None) -> bool:
    """Is this source actually about the page we were asked to crawl?

    Applies to every source that lives at the site root - a probed feed, a CMS
    API - because they share one failure: they answer for the whole site while
    the target is one section of it.

    Path probing looks for a feed at the SITE ROOT, which is right when the
    target is the site's newsroom and wrong when it is one section of a larger
    site. fightcancer.org's press room is at /press-room/search; probing found
    the organisation-wide /rss.xml, whose ten newest items are advocacy
    actions, events and legislative summaries. Every one of them is a real
    article, so nothing downstream could tell anything was wrong - the crawler
    simply collected the wrong ten documents and reported success. That is the
    silent-failure shape this whole project is built to catch.

    A feed the page itself declares is authoritative, so it is trusted outright.
    A probed root feed has to corroborate: at least one of its items must be
    linked from the target page. Zero overlap across a whole feed means it
    covers a different part of the site.
    """
    if declared or not section or not listing_html:
        return True

    # Same-site only, because that is what we are comparing against: a
    # discovered URL is this publisher's by construction. Counting every link
    # made the thin-page guard below useless - northernvermont.edu's newsroom
    # exposes 120 links, of which 6 are its own and the rest are social and
    # portal links to other domains entirely.
    target_domain = registrable_domain(newsroom_url)
    base = base_url or newsroom_url
    page_links = {canonical_url(urljoin(base, a.attributes.get("href") or ""))
                  for a in LexborHTMLParser(listing_html).css("a[href]")}
    page_links = {u for u in page_links
                  if u and registrable_domain(u) == target_domain}

    # Absence of evidence is not evidence of absence. A JS-rendered newsroom
    # whose article list loads after paint exposes almost no links in the HTML
    # we hold, so there is nothing to corroborate against - and rejecting on
    # that basis throws away a source that was right.
    #
    # northernvermont.edu paid for exactly this: its `/wp-json` was returning
    # real articles, corroboration found no overlap because the page had under
    # twenty links at all, and discovery fell through to a sitemap serving a
    # category index and a 2021 dean's list.
    if len(page_links) < MIN_PAGE_LINKS_TO_JUDGE:
        return True
    return any(ref.url in page_links for ref in found.articles)


async def discover(
    fetcher: PoliteFetcher,
    newsroom_url: str,
    *,
    known_feed: str | None = None,
    known_method: str | None = None,
    feed_absent: bool = False,
    limit: int = 50,
) -> Discovery:
    """Find recent articles for one newsroom URL.

    `known_feed`/`known_method`/`feed_absent` come from the frontier, so a
    domain solved on a previous run skips straight to the winning source
    instead of re-probing every path on every run.
    """
    base = _origin(newsroom_url)
    seed = canonical_url(newsroom_url)
    target_domain = registrable_domain(seed)
    section = urlsplit(seed).path.rstrip("/").lower() or None
    if section and (section.count("/") < 1 or len(section) < 3):
        section = None          # site root is not a useful section hint

    # Assigned by the listing fast path when it runs, so the full cascade below
    # reuses that response instead of fetching the same page twice.
    listing_html: str | None = None
    # The URL that HTML actually came from. Relative links resolve against this,
    # never against `newsroom_url` - see harvest_links for what that cost.
    listing_url: str = newsroom_url
    # Set once the newsroom page has been requested, so a fast path that tried
    # it and failed does not make the cascade pay for the same refusal twice.
    listing_fetched: bool = False
    # Why it is not in hand, when it is not. See the cascade's fetch below.
    listing_error: str | None = None

    def usable(found: Discovery) -> bool:
        """Did this method find anything the crawl would actually keep?

        Not "did it return something" - that is how a source gets accepted on
        results the next stage throws away. Two vetoes are applied here, both
        of which the crawl applies anyway, so a source that only satisfies the
        letter of "found articles" does not get to win:

        **The listing page itself.** Sitemaps routinely contain the newsroom
        URL and nothing else from that section. Counting it as a hit made
        discovery report success, cache "sitemap works", and then yield zero
        articles once the crawl gates dropped it - so the listing-page fallback
        was never reached. battelle.org failed exactly this way.

        **Another publisher's content.** ufw.org runs a press-clippings feed:
        its `/wp-json` returns 28 links to Courthouse News, HuffPost, Newsweek
        and the Washington Post out of 30. All real articles, none of them
        UFW's. Discovery declared success on the surviving 2 and never tried
        the listing page, which carries 24 actual UFW press releases.
        """
        total = len(found.articles)
        found.articles = [a for a in found.articles
                          if canonical_url(a.url) != seed
                          and registrable_domain(a.url) == target_domain]
        dropped = total - len(found.articles)
        # Ratio, not a count: one syndicated link in a feed is ordinary, and a
        # small result set should not be condemned by a single stray. The floor
        # keeps a 2-of-3 sample from tripping it.
        if total >= MIN_RESULTS_FOR_RATIO and len(found.articles) / total < MIN_OWN_CONTENT:
            found.notes.append(
                f"ignoring {found.method}: {dropped} of {total} results are not "
                f"{target_domain}")
            return False
        return bool(found.articles)

    # ---- fast path -----------------------------------------------------
    # A domain solved on a previous run goes straight back to the source that
    # worked. This is the difference between one request and fourteen: without
    # it, battelle.org re-probed nine dead feed paths and re-walked a sitemap
    # index on every single daily run.
    if known_method == "cms_api" and not section:
        # Only when the target IS the site root. `/wp-json/wp/v2/posts` always
        # answers for the whole site, so on a sectioned target it is the same
        # untrusted shape as a root feed - and here there is no page in hand to
        # corroborate against. Falling through costs one extra request (the
        # cascade fetches the listing page it would need anyway, then calls
        # wp-json at step 1) and buys the corroboration. Skipping that is how
        # aacr.org kept serving its blog to a /newsroom/news-releases target.
        found = await from_wp_json(fetcher, base, limit=limit)
        if usable(found):
            return found
    elif known_method == "rss" and known_feed and not _root_feed_for_section(
            known_feed, section):
        found = await from_feed(fetcher, known_feed)
        if usable(found):
            return found
    elif known_method in ("sitemap", "news_sitemap") and known_feed:
        found = await from_sitemap(fetcher, base, limit=limit,
                                   known_sitemap=known_feed, section=section)
        if usable(found):
            return found
    elif known_method == "listing":
        # The listing page is a last resort to *reach*, but once a target has
        # settled on it there is nothing cheaper: the page is one request and
        # it is the request the full cascade would open with anyway. Without
        # this branch a listing target re-walked the sitemap index on every
        # single run, having already established the sitemap does not cover it.
        resp = await fetcher.get(newsroom_url)
        listing_fetched = True
        if resp.ok:
            listing_html = resp.text
            listing_url = resp.final_url or newsroom_url
            found = await from_listing(fetcher, newsroom_url, listing_html,
                                       limit=limit, section=section,
                                       base_url=listing_url)
            if usable(found):
                return found
        elif resp.wall:
            out = Discovery(method="none")
            out.errors.append(f"bot wall: {resp.wall}")
            return out
        else:
            listing_error = f"newsroom page: {resp.error or f'HTTP {resp.status}'}"
    # Anything that falls through here re-runs the full cascade, which is what
    # should happen when a site changes CMS or moves its feed.

    probe_notes: list[str] = []
    # Why the target's own page is not in hand, when it is not. The listing page
    # is only one of four sources, so its failure is not fatal - a sitemap may
    # still answer - but it has to be reported. lawsociety.org.uk returns a
    # branded 403 ("403 - The Law Society"), which is not a solvable challenge
    # and so is correctly not a bot wall; discovery then ran the whole cascade
    # against a site refusing us and concluded "all discovery methods failed".
    # True, and useless: it names the symptom and hides the cause.
    if listing_html is None and not listing_fetched:
        resp = await fetcher.get(newsroom_url)
        if resp.ok:
            listing_html = resp.text
            listing_url = resp.final_url or newsroom_url
        elif resp.wall:
            out = Discovery(method="none")
            out.errors.append(f"bot wall: {resp.wall}")
            return out
        else:
            listing_error = (f"newsroom page: "
                             f"{resp.error or f'HTTP {resp.status}'}")

    # 1. WordPress REST - one request for headline + body + exact date.
    #    Corroborated like any other site-root source: `/wp-json/wp/v2/posts`
    #    is exactly as site-wide as `/rss.xml`, and aacr.org's serves the blog
    #    while the target is /about-the-aacr/newsroom/news-releases. Real
    #    articles, wrong section, no error - the same silent shape.
    if listing_html and ("/wp-content/" in listing_html or "/wp-json" in listing_html):
        found = await from_wp_json(fetcher, base, limit=limit)
        if usable(found):
            if _covers_target(found, listing_html, newsroom_url, section, False,
                              base_url=listing_url):
                return found
            probe_notes.append(
                f"ignoring site-wide CMS API: none of its posts appear under {section}")

    # 2. Feeds. Autodiscovery always runs; path probing is skipped when a
    #    previous run already proved there is nothing to find.
    # Probing is skipped when a previous run already found the feed - we know
    # the answer, we just are not going to trust it blindly. Autodiscovery
    # still runs, since it is free and a declared feed outranks a probed one.
    feed_url, probed_empty, declared = await find_feed(
        fetcher, newsroom_url, listing_html,
        skip_probe=feed_absent or bool(known_feed), base_url=listing_url)
    if feed_url is None and known_feed and known_method == "rss":
        feed_url, declared = known_feed, False
    if feed_url:
        found = await from_feed(fetcher, feed_url)
        if usable(found) and _covers_target(
                found, listing_html, newsroom_url, section, declared,
                base_url=listing_url):
            return found
        if usable(found):
            out_note = (f"ignoring site-wide feed {feed_url}: none of its items "
                        f"appear under {section}")
            probe_notes.append(out_note)

    # 3. Sitemaps.
    found = await from_sitemap(fetcher, base, limit=limit, section=section)
    if usable(found) and (found.scoped or not section):
        found.feed_absent = probed_empty
        found.notes.extend(probe_notes)
        return found

    # A sitemap that could not be scoped to the target's section is held in
    # reserve. It is a whole-site dump, so the target's own listing page - an
    # explicit statement of what belongs in this section - outranks it.
    unscoped_sitemap = found if usable(found) else None
    if unscoped_sitemap is not None:
        probe_notes.append(
            f"sitemap matched nothing under {section}; preferring the listing page")

    # 4. Listing page.
    if listing_html:
        found = await from_listing(fetcher, newsroom_url, listing_html,
                                   limit=limit, section=section,
                                   base_url=listing_url)
        found.feed_absent = probed_empty
        found.notes.extend(probe_notes)
        if usable(found):
            return found

    if unscoped_sitemap is not None:
        unscoped_sitemap.feed_absent = probed_empty
        unscoped_sitemap.notes.extend(probe_notes)
        return unscoped_sitemap

    out = Discovery(method="none", feed_absent=probed_empty)
    out.notes.extend(probe_notes)
    if listing_error:
        out.errors.append(listing_error)
    out.errors.append("all discovery methods failed")
    return out
