"""The extraction cascade.

Ordered by cost and trustworthiness, stopping as soon as a rung succeeds:

    0. cached per-domain wrapper (Phase 5)
    1. JSON-LD   (Article / BlogPosting / NewsArticle / Report)
    2. microdata / <time pubdate>
    3. OpenGraph / Twitter / bare meta
    4. trafilatura                      <- body of record
    5. htmldate(original_date=True)     <- date of record
    6. resiliparse                      <- second opinion, quality signal only
    7. escalate to browser / LLM induction
    8. quarantine

Fields are resolved *independently*, not by picking one winning rung: the
survey found article-type JSON-LD on only ~24% of pages while trafilatura
produced usable text on ~95%, so the headline usually comes from metadata and
the body almost always comes from trafilatura.
"""

from __future__ import annotations

from datetime import datetime

from selectolax.lexbor import LexborHTMLParser

from .body import (
    clean_body,
    looks_like_navigation,
    prose_ratio,
    detect_language,
    extract_body,
    extract_body_second_opinion,
    token_overlap,
)
from .dates import resolve_date
from .metadata import (
    extract_jsonld,
    extract_meta,
    extract_microdata_dates,
    headline_from_dom,
)
from .models import Article, Path

MIN_BODY_CHARS = 300
# Below this, a "successful" HTTP 200 almost certainly rendered nothing useful.
JS_SHELL_TEXT_CHARS = 200

_HYDRATION_MARKERS = ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__",
                      "__APOLLO_STATE__", "__remixContext")


def needs_browser(html: str, body: str | None, has_structured_body: bool) -> tuple[bool, str | None]:
    """Decide escalation on OUTCOME, never on framework detection.

    Many Next.js and Nuxt news sites server-render perfectly well; routing to a
    browser on an `id="__next"` marker would pay ~10x the cost for nothing. A
    hydration payload is reported separately because the article text is often
    already inside it - mining that turns a browser page load into a JSON parse.
    """
    text_len = len(body or "")
    if text_len >= JS_SHELL_TEXT_CHARS or has_structured_body:
        return False, None
    if not html:
        return True, "empty response"
    lowered = html.lower()
    if "enable javascript" in lowered or "requires javascript" in lowered:
        return True, "noscript js-required notice"
    if len(html) < 1000:
        return True, "html under 1KB"
    if any(m in html for m in _HYDRATION_MARKERS):
        return True, "hydration payload present - mine it before launching a browser"
    return True, f"low text yield ({text_len} chars)"


def extract_article(
    html: str,
    url: str,
    *,
    feed_headline: str | None = None,
    feed_date: str | None = None,
    feed_body: str | None = None,
    sitemap_lastmod: str | None = None,
    http_last_modified: str | None = None,
    fetched_at: datetime | None = None,
    second_opinion: bool = False,
    site_name: str | None = None,
) -> Article:
    """Extract headline, body, and date from one article page."""
    article = Article(url=url)

    if not html:
        article.warnings.append("empty html")
        article.quality["needs_browser"] = True
        return article

    tree = LexborHTMLParser(html)
    facts = extract_jsonld(tree)
    meta = extract_meta(tree)
    micro = extract_microdata_dates(tree)

    # ---- headline ----------------------------------------------------
    # Feed and CMS-API titles are publisher-asserted and beat anything scraped.
    for source, value in (
        (Path.FEED, feed_headline),
        (Path.JSONLD, facts.headline),
        (Path.OPENGRAPH, meta.get("og:title") or meta.get("twitter:title")),
    ):
        if value and value.strip():
            article.headline = value.strip()
            article.headline_source = source
            break
    if not article.headline:
        dom_headline = headline_from_dom(tree, site_name=site_name or meta.get("og:site_name"))
        if dom_headline:
            article.headline = dom_headline
            article.headline_source = Path.TRAFILATURA

    # ---- body --------------------------------------------------------
    raw_body = extract_body(html, url=url)
    body_source = Path.TRAFILATURA

    # A feed carrying content:encoded gives the full body with no article fetch
    # at all, so prefer it when it is at least as complete.
    if feed_body and len(feed_body) > len(raw_body or ""):
        raw_body, body_source = feed_body, Path.FEED

    # JSON-LD articleBody appeared ZERO times in 94 surveyed article pages.
    # Read it when present, but only when it is longer AND consistent - a
    # paywall stub would otherwise silently replace real extracted text.
    if facts.body and len(facts.body) > len(raw_body or ""):
        if token_overlap(facts.body, raw_body) >= 0.4 or not raw_body:
            raw_body, body_source = facts.body, Path.JSONLD

    article.body = clean_body(raw_body)
    # Extractors routinely include the <h1>, leaving the headline duplicated as
    # the body's first line. Strip it so the stored body is the article text
    # only - and so "headline == first line of body" stays usable as a signal
    # that extraction started too high in the DOM.
    article.body = _strip_leading_headline(article.body, article.headline)
    article.body_source = body_source if article.body else Path.NONE

    # ---- date --------------------------------------------------------
    article.date = resolve_date(
        html=html,
        url=url,
        jsonld_raw=facts.date_raw,
        jsonld_is_weak=facts.weak_only,
        meta=meta,
        microdata_raw=micro,
        feed_raw=feed_date,
        sitemap_lastmod=sitemap_lastmod,
        http_last_modified=http_last_modified,
        fetched_at=fetched_at,
    )

    # ---- language ----------------------------------------------------
    article.language = detect_language(article.body) or (
        facts.language or meta.get("og:locale") or None)

    # ---- quality signals --------------------------------------------
    escalate, reason = needs_browser(html, article.body, bool(facts.body))
    article.quality.update({
        "body_len": article.body_len,
        "html_len": len(html),
        "jsonld_is_article": facts.is_article,
        "jsonld_weak_only": facts.weak_only,
        "jsonld_types": sorted(facts.types),
        "needs_browser": escalate,
        "needs_browser_reason": reason,
        "headline_source": article.headline_source.value,
        "body_source": article.body_source.value,
        "date_source": article.date.source.value,
    })

    if second_opinion:
        other = extract_body_second_opinion(html)
        overlap = token_overlap(article.body, other)
        article.quality["second_opinion_overlap"] = round(overlap, 3)
        article.quality["second_opinion_len"] = len(other or "")
        # Two unrelated extractors disagreeing is the cheapest available
        # signal that one of them grabbed the wrong subtree.
        if other and overlap < 0.5 and len(other) > MIN_BODY_CHARS:
            article.warnings.append(f"low second-opinion overlap ({overlap:.2f})")

    article.quality["headline_in_body"] = _headline_coherence(article)
    article.quality["prose_ratio"] = prose_ratio(article.body)
    if article.body and looks_like_navigation(article.body):
        article.quality["looks_like_navigation"] = True
        article.warnings.append(
            f"body looks like page chrome (prose ratio {article.quality['prose_ratio']})")

    # ---- warnings ----------------------------------------------------
    if not article.headline:
        article.warnings.append("no headline")
    elif len(article.headline) > 250:
        article.warnings.append("headline over 250 chars")
    if article.body_len < MIN_BODY_CHARS:
        article.warnings.append(f"body under {MIN_BODY_CHARS} chars")
    if article.date.value is None:
        article.warnings.append("no date")
    elif article.date.disagreement_days:
        article.warnings.append(
            f"date sources disagree by {article.date.disagreement_days}d")
    if escalate:
        article.warnings.append(f"needs browser: {reason}")

    return article


def _strip_leading_headline(body: str | None, headline: str | None) -> str | None:
    """Remove the headline when it is repeated as the body's opening line."""
    if not body or not headline:
        return body
    lines = body.split("\n", 1)
    first = lines[0].strip().rstrip(".:").lower()
    target = headline.strip().rstrip(".:").lower()
    if first and (first == target or (len(first) < 250 and first.startswith(target))):
        rest = lines[1].lstrip("\n") if len(lines) > 1 else ""
        return rest.strip() or body
    return body


def _headline_coherence(article: Article) -> float | None:
    """Fraction of headline content words appearing early in the body.

    Catches the specific failure of pairing the right headline with the wrong
    article container - which returns a plausible-looking record that is simply
    about a different story.
    """
    if not article.headline or not article.body:
        return None
    import re

    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "at",
            "by", "with", "from", "as", "is", "are", "was", "were", "be", "its"}
    words = {w for w in re.findall(r"\w+", article.headline.lower())
             if len(w) > 2 and w not in stop}
    if not words:
        return None
    lead = article.body[:1500].lower()
    hits = sum(1 for w in words if w in lead)
    return round(hits / len(words), 3)
