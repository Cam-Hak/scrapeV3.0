"""URL canonicalization, registrable-domain extraction, and article/listing
classification.

Two things here are load-bearing for the whole system:

1. `registrable_domain` must use the public suffix list. Naive "last two
   labels" breaks bbc.co.uk, nhk.or.jp and thousands of ccTLDs - and the v2
   corpus is 635 .edu / 632 .gov / 115 .ca+.uk, so those cases are the norm,
   not the exception. This is the politeness pacing key AND the shard key; if
   it is wrong we either hammer a host or split one host across two workers.

2. `canonical_url` produces the dedup key. v2 deduped on a synthesized
   filename ("$H" + agency + YYMMDD + headline[-10:]) which silently collided
   on same-day articles and re-inserted whenever an editor fixed a headline.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import tldextract

# Cache the public suffix list on disk; refreshed manually, never at import
# time in a worker (a cold PSL fetch inside a crawl loop is a nasty surprise).
_extract = tldextract.TLDExtract(suffix_list_urls=())

# Tracking parameters carry no page identity. Stripping them collapses many
# apparent duplicates - important because syndicated press releases arrive with
# per-outlet campaign tags.
_JUNK_PARAMS = re.compile(
    r"^(utm_|ic[ei]d$|mc_|pk_|fbclid$|gclid$|dclid$|msclkid$|yclid$|_ga$|_gl$"
    r"|ref$|referrer$|source$|cmpid$|CMP$|sr_share$|at_medium$|at_campaign$)",
    re.IGNORECASE,
)

_DATE_IN_PATH = re.compile(r"/(19|20)\d{2}[/-](0?[1-9]|1[0-2])([/-](0?[1-9]|[12]\d|3[01]))?(/|$|-)")
_COMPACT_DATE = re.compile(r"/(19|20)\d{6}(/|$|-)")

# Hard listing markers anywhere in the path.
_LISTING_PATH = re.compile(
    r"(^|/)(category|categories|tag|tags|topic|topics|author|authors|archive|archives"
    r"|page|pages|search|feed|rss|amp|print)(/|$)",
    re.IGNORECASE,
)

# Section landing pages. These are only listings when they are the LAST
# segment - "/newsroom" is an index, but "/newsroom/mayor-announces-plan" is an
# article. Caught in production: battelle.org's own newsroom URL scored as an
# article and was stored with the site's nav text as its body.
_SECTION_INDEX = re.compile(
    r"^(news|newsroom|press|press-releases|press-release|pressroom|press-room"
    r"|media|media-centre|media-center|media-room|blog|blogs|insights|stories"
    r"|updates|announcements|publications|resources|events|latest|all-news"
    r"|news-releases|news-release|newsreleases|releases)$",
    re.IGNORECASE,
)
_LISTING_QUERY = re.compile(r"^(page|paged|p|offset|start|pagenum)$", re.IGNORECASE)
# A four-digit year standing alone as the last path segment: /news/2026. An
# archive index, not an article, however much it resembles a numeric permalink.
_BARE_YEAR = re.compile(r"(19|20)\d{2}")

# Content that is not news, whatever its URL shape. Institutional sites - 53%
# of this corpus is .edu/.gov - commonly publish one site-wide feed mixing
# press releases with events, staff profiles and course pages.
# edisonohio.edu's /News feed yielded /event/2026-08/welcome-week, a campus
# event, which is not a press release however article-shaped the URL looks.
_NON_ARTICLE_SECTION = re.compile(
    r"(^|/)(event|events|calendar|webinar|webinars|conference"
    r"|people|person|staff|faculty|directory|profile|profiles|bio|bios"
    r"|course|courses|program|programs|programme|degree|degrees|curriculum"
    r"|job|jobs|career|careers|vacancy|vacancies|employment"
    r"|location|locations|campus|facility|facilities"
    r"|product|products|service|services|shop|store|cart|checkout"
    r"|login|register|account|donate|give|giving|contact|privacy|terms"
    r"|faq|faqs|sitemap|search-results)(/|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Site:
    """A target newsroom URL with its derived keys."""

    newsroom_url: str
    domain: str          # eTLD+1 - the politeness and shard key
    host: str            # full hostname, for building absolute URLs

    @property
    def origin(self) -> str:
        parts = urlsplit(self.newsroom_url)
        return f"{parts.scheme}://{parts.netloc}"

# CAM: function to recieve the domain from the entire link
# This is used to automate the process of politeness per domain
def registrable_domain(url_or_host: str) -> str:
    """Return the eTLD+1 (e.g. 'bbc.co.uk' from 'news.bbc.co.uk/x').

    Falls back to the raw host when the suffix list cannot classify it, which
    happens for intranet names and bare IPs. Returning something stable matters
    more than returning something correct here - it is a partition key.
    """
    host = url_or_host
    if "//" in url_or_host or url_or_host.startswith(("http:", "https:")):
        host = urlsplit(url_or_host).hostname or ""
    host = host.strip().lower().rstrip(".")
    if not host:
        return ""
    ext = _extract(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


# CAM: add url to the articles.sqlite to check its uniqueness
# saves future requests in cases where you need to traverse
# link to check if headline and date have been stored before
def _strip_www(host: str) -> str:
    """Drop a leading `www.`, but only when it is genuinely decorative.

    `www.crnusa.org` and `crnusa.org` are one site, and a sitemap that emits
    one form while the page links the other produced two different URL hashes
    for one article - stored twice, fetched twice.

    The guard matters: `www.gov.uk` IS the registrable domain, because `gov.uk`
    is a public suffix. Stripping there would turn a real site into a suffix
    that belongs to nobody. So strip only when doing so leaves the registrable
    domain unchanged.
    """
    if not host.startswith("www."):
        return host
    bare = host[4:]
    return bare if registrable_domain(bare) == registrable_domain(host) else host


def canonical_url(url: str, base: str | None = None) -> str:
    """Normalize a URL into its dedup key form.

    Lowercases scheme/host, drops the fragment, strips tracking parameters,
    sorts the remaining query, removes a trailing slash and default ports.
    Deliberately does NOT strip a trailing 'index.html' - some CMSes serve
    different content there.
    """
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    host = _strip_www(host)

    netloc = host
    if parts.port and not ((scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)):
        netloc = f"{host}:{parts.port}"

    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
               if not _JUNK_PARAMS.match(k)),
    )

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path or "/", query, ""))


def url_hash(url: str) -> bytes:
    """sha256 of the canonical URL - the primary dedup key."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).digest()


def make_site(newsroom_url: str) -> Site:
    url = canonical_url(newsroom_url)
    return Site(
        newsroom_url=url,
        domain=registrable_domain(url),
        host=urlsplit(url).hostname or "",
    )


# ---------------------------------------------------------------------------
# Article vs listing classification
# ---------------------------------------------------------------------------

@dataclass
class UrlVerdict:
    score: float          # >0 leans article, <0 leans listing
    is_article: bool
    reasons: list[str]


# Segments that mark a path as news-bearing, used to rescue URLs that would
# otherwise be vetoed by a broad section name (e.g. "/about/news/story-slug").
_NEWS_SEGMENT = re.compile(
    r"(^|/)(news|newsroom|press|press-release[s]?|news-release[s]?|releases"
    r"|story|stories|article|articles|blog|media|announcement[s]?|bulletin)(/|$)",
    re.IGNORECASE,
)

# Broad institutional sections that are only non-news when nothing on the path
# says otherwise. "/about/edison-foundation/1879-society" is a static page;
# "/about/news/mayor-visits" is a press release.
_SOFT_NON_NEWS = re.compile(
    r"(^|/)(about|about-us|who-we-are|our-story|leadership|governance"
    r"|foundation|alumni|admissions|academics|research-areas|support-us)(/|$)",
    re.IGNORECASE,
)


def is_non_news_path(url: str) -> bool:
    """True when the URL is structurally not news, whatever its shape.

    Applied to EVERY discovered reference, including feed and CMS-API items.
    Feeds are otherwise trusted because they only publish articles - but plenty
    of institutional sites run one site-wide feed that mixes press releases
    with events, staff profiles and static "about" pages.

    Two tiers, because the evidence called for it:
      * hard sections (/event/, /people/, /courses/) are never news;
      * soft sections (/about/, /foundation/) are usually not news, but are
        rescued when the path also carries a news segment. edisonohio.edu's
        /about/edison-foundation/1879-society reached the store because
        "1879-society" is article-shaped enough to pass every other check.
    """
    path = urlsplit(canonical_url(url)).path
    if _NON_ARTICLE_SECTION.search(path):
        return True
    if _SOFT_NON_NEWS.search(path) and not _NEWS_SEGMENT.search(path):
        return True
    return False


def classify_url(url: str) -> UrlVerdict:
    """Score a URL as article-like or listing-like from its shape alone.

    URL-only signals; DOM signals (linked-headline ratio, JSON-LD @type) are
    stronger and are applied later by the discovery layer. This runs first
    because it is free and rules out obvious category/tag/pagination pages
    before we spend a request on them.
    """
    parts = urlsplit(canonical_url(url))
    path = parts.path
    score = 0.0
    reasons: list[str] = []

    if _LISTING_PATH.search(path):
        score -= 3.0
        reasons.append("listing path segment")

    # Hard veto: not news, regardless of how article-shaped the rest looks.
    # Weighted heavily enough that a date in the path cannot outvote it.
    if _NON_ARTICLE_SECTION.search(path):
        score -= 10.0
        reasons.append("non-news section")
    elif _SOFT_NON_NEWS.search(path) and not _NEWS_SEGMENT.search(path):
        score -= 10.0
        reasons.append("static institutional section")

    if any(_LISTING_QUERY.match(k) for k, _ in parse_qsl(parts.query)):
        score -= 3.0
        reasons.append("pagination query param")

    segments = [s for s in path.split("/") if s]

    # A section index like /newsroom or /press-releases. Only a listing when it
    # is the final segment.
    if segments and _SECTION_INDEX.match(segments[-1]):
        score -= 3.0
        reasons.append(f"section index: /{segments[-1]}")

    # Positive evidence that this is a specific article, as opposed to merely
    # not looking like a listing. Requiring at least one of these is what stops
    # a bare section page from qualifying on path depth alone.
    has_article_signal = False

    if _DATE_IN_PATH.search(path) or _COMPACT_DATE.search(path):
        score += 2.5
        has_article_signal = True
        reasons.append("date in path")

    if len(segments) >= 3:
        score += 0.5
        reasons.append("deep path")
    elif len(segments) <= 1:
        score -= 1.0
        reasons.append("shallow path")

    if segments:
        slug = segments[-1]
        # Strip a file extension before counting words.
        slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.IGNORECASE)
        words = [w for w in re.split(r"[-_]", slug) if w]
        # Threshold is 3, not 4: real article slugs are frequently three
        # words ("rewriting-story-metal"), while the section indexes we must
        # reject are one or two ("press-releases", "media-center", "all-news").
        if len(words) >= 3 and not slug.isdigit():
            score += 2.0
            has_article_signal = True
            reasons.append(f"slug has {len(words)} words")
        elif len(words) == 1 and not slug.isdigit():
            score -= 0.5
            reasons.append("single-word slug")
        # A bare numeric id is a common article permalink on older CMSes -
        # /node/352363. A bare YEAR is the opposite: /news/2026 is a date
        # archive index, and hccs.edu's sitemap is full of them. Both are four
        # or more digits, so the id rule claimed the archives too and they
        # reached extraction as "articles".
        if slug.isdigit() and len(slug) >= 4:
            if _BARE_YEAR.fullmatch(slug):
                score -= 3.0
                reasons.append("date archive index")
            else:
                score += 1.0
                has_article_signal = True
                reasons.append("numeric article id")

    if not has_article_signal:
        reasons.append("no positive article signal")

    return UrlVerdict(
        score=score,
        is_article=score > 0 and has_article_signal,
        reasons=reasons,
    )
