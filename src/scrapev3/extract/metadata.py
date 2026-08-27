"""Structured-metadata extraction: JSON-LD, OpenGraph, microdata, meta tags.

Two decisions here come straight from measuring the real corpus rather than
from the literature:

1. **Match four article types, not one.** Of the article-type JSON-LD found in
   the survey sample: NewsArticle 12, Article 12, BlogPosting 2. Matching only
   `NewsArticle` - the obvious choice - would have missed 48% of it.

2. **Accept `WebPage` for headline and date, but never for body.** Only ~24% of
   article pages carried article-type JSON-LD, while `WebPage` appeared on
   ~40%. `WebPage` inherits `datePublished`/`headline` from CreativeWork, so it
   is a legitimate date source. It is *not* evidence that the page is an
   article, so it is tracked at lower confidence and never supplies body text.

Also measured: `articleBody` appeared **zero times in 94 article pages**. It is
still read when present, but nothing in the design may depend on it.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from selectolax.lexbor import LexborHTMLParser

# Types that assert "this page is an article".
ARTICLE_TYPES = {
    "newsarticle", "article", "blogposting", "report", "pressrelease",
    "reportagenewsarticle", "analysisnewsarticle", "backgroundnewsarticle",
    "opinionnewsarticle", "reviewnewsarticle", "liveblogposting", "socialmediaposting",
}

# Types that may carry usable headline/date but do NOT assert articleness.
WEAK_TYPES = {"webpage", "collectionpage", "itempage", "aboutpage", "creativework"}

_HEADLINE_KEYS = ("headline", "name", "alternativeHeadline")
_DATE_KEYS = ("datePublished", "dateCreated", "uploadDate", "datePosted")


def iter_jsonld(tree: LexborHTMLParser) -> Iterator[dict[str, Any]]:
    """Yield every JSON-LD object, flattening @graph and nested arrays."""
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Malformed JSON-LD is common; a broken block must never abort
            # extraction for the whole page.
            continue
        stack: list[Any] = [data]
        seen = 0
        while stack and seen < 500:      # guard against pathological nesting
            item = stack.pop()
            seen += 1
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                yield item


def types_of(obj: dict[str, Any]) -> set[str]:
    t = obj.get("@type")
    if isinstance(t, str):
        return {t.lower()}
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return set()


def _first_str(obj: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for v in value:
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


class JsonLdFacts:
    """What the page's JSON-LD asserts, with confidence attached."""

    def __init__(self) -> None:
        self.is_article = False          # an ARTICLE_TYPES object was present
        self.headline: str | None = None
        self.date_raw: str | None = None
        self.body: str | None = None
        self.language: str | None = None
        self.types: set[str] = set()
        self.weak_only = False           # only WebPage-ish types carried data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"JsonLdFacts(is_article={self.is_article}, weak_only={self.weak_only}, "
                f"headline={self.headline!r}, date={self.date_raw!r}, "
                f"body_len={len(self.body or '')})")


def extract_jsonld(tree: LexborHTMLParser) -> JsonLdFacts:
    facts = JsonLdFacts()
    weak_headline: str | None = None
    weak_date: str | None = None

    for obj in iter_jsonld(tree):
        types = types_of(obj)
        if not types:
            continue
        facts.types |= types

        if types & ARTICLE_TYPES:
            facts.is_article = True
            facts.headline = facts.headline or _first_str(obj, _HEADLINE_KEYS)
            facts.date_raw = facts.date_raw or _first_str(obj, _DATE_KEYS)
            body = obj.get("articleBody")
            if isinstance(body, str) and len(body.strip()) > 200:
                facts.body = body.strip()
            lang = obj.get("inLanguage")
            if isinstance(lang, str):
                facts.language = lang
            elif isinstance(lang, dict):
                facts.language = lang.get("name") or lang.get("@id")

        elif types & WEAK_TYPES:
            weak_headline = weak_headline or _first_str(obj, _HEADLINE_KEYS)
            weak_date = weak_date or _first_str(obj, _DATE_KEYS)

    # Fall back to WebPage-ish data only where the article types gave nothing.
    if not facts.headline and weak_headline:
        facts.headline = weak_headline
        facts.weak_only = True
    if not facts.date_raw and weak_date:
        facts.date_raw = weak_date
        facts.weak_only = True

    return facts


def extract_meta(tree: LexborHTMLParser) -> dict[str, str]:
    """Collect OpenGraph, Twitter card, and bare meta tags into one dict.

    Keys are lowercased and the og:/twitter: prefixes are preserved so callers
    can tell an authoritative `article:published_time` from a generic `date`.
    """
    out: dict[str, str] = {}
    for node in tree.css("meta"):
        attrs = node.attributes
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        content = attrs.get("content")
        if not key or not content:
            continue
        key = key.strip().lower()
        content = content.strip()
        if content and key not in out:
            out[key] = content
    return out


def extract_microdata_dates(tree: LexborHTMLParser) -> str | None:
    """Pull a publish date from microdata / <time> markup.

    Only elements that name themselves as a publish date are trusted -
    `<time>` alone appears all over sidebars and "related stories" teasers, and
    grabbing those is exactly how v2 ended up recording bylines as dates.
    """
    for selector in (
        'meta[itemprop="datePublished"]',
        '[itemprop="datePublished"]',
        'time[itemprop="datePublished"]',
        'time[pubdate]',
        'time.published',
        'time.entry-date',
    ):
        node = tree.css_first(selector)
        if node is None:
            continue
        value = (node.attributes.get("content")
                 or node.attributes.get("datetime")
                 or node.text(strip=True))
        if value:
            return value.strip()
    return None


def headline_from_dom(tree: LexborHTMLParser, site_name: str | None = None) -> str | None:
    """Last-resort headline: the first <h1>, else <title> minus the site suffix."""
    h1 = tree.css_first("h1")
    if h1 is not None:
        text = h1.text(strip=True)
        if text and 10 <= len(text) <= 250:
            return text

    title_node = tree.css_first("title")
    if title_node is None:
        return None
    title = title_node.text(strip=True)
    if not title:
        return None

    return headline_from_title(title, site_name)


# Section labels that appear in <title> chrome but are never the headline.
_TITLE_CHROME = {
    "news", "newsroom", "press", "press releases", "press release", "pressroom",
    "press room", "media", "media centre", "media center", "blog", "blogs",
    "insights", "stories", "updates", "announcements", "home", "latest",
    "news releases", "news release", "articles", "publications", "events",
}

_TITLE_SEPARATORS = (" | ", " - ", " — ", " – ", " :: ", " » ", " > ", "|")


def headline_from_title(title: str, site_name: str | None = None) -> str:
    """Pull the headline out of a <title>, whichever end the chrome is on.

    Splitting on the last separator assumes "Headline | Site", which is the
    common form - but plenty of sites use "Site | Section | Headline".
    centerforfoodsafety.org produced
    "Center for Food Safety | Press Releases | | Lawsuit Filed to Stop..."
    and the naive rule kept exactly the wrong half.

    So: split on every separator, drop the site name and known section labels,
    and keep the longest remaining part. Headlines are essentially always the
    longest component of a title.
    """
    parts: list[str] = [title]
    for sep in _TITLE_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(sep)]

    cleaned: list[str] = []
    site = (site_name or "").strip().lower()
    for part in parts:
        candidate = part.strip()
        if not candidate:
            continue
        low = candidate.lower()
        if site and low == site:
            continue
        if low in _TITLE_CHROME:
            continue
        cleaned.append(candidate)

    if not cleaned:
        return title.strip()
    # Longest surviving segment. Ties keep the earliest, which is the safer
    # choice for the "Headline | Site" majority case.
    return max(cleaned, key=len)
