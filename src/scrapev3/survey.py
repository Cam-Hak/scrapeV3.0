"""Phase 1 - measure the universe.

Runs the *real* discovery and extraction path against a sample of target
newsroom URLs and records what actually happened, per domain, as JSONL.

The plan flags five numbers as unmeasured inference that everything else
depends on. This tool produces all five:

  1. JS-escalation rate      - does plain HTTP yield article text?
  2. JSON-LD coverage        - the "68% of media sites" figure circulating in
                               2026 is SEO content marketing with no primary
                               source. Measure it; do not plan against it.
  3. articleBody completeness- how often structured data carries the full body
  4. /wp-json reachability   - no published measurement exists
  5. discovery coverage      - how the sitemap/feed/CMS/listing cascade stacks

It also records shared-IP concentration, which decides whether per-domain
pacing is sufficient or whether we additionally need to pace per IP (Nutch
queues on (host, IP) for exactly this reason).

This is a measurement tool, not a crawler: it touches each domain a handful of
times, at full politeness, and never revisits.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from .fetch import PoliteFetcher, Response
from .settings import Settings
from .urls import Site, canonical_url, classify_url, make_site, registrable_domain

# Sitemap paths worth probing when robots.txt names none.
SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/wp-sitemap.xml",          # WordPress 5.5+ core
    "/sitemap-index.xml",
    "/news-sitemap.xml",
    "/sitemap/sitemap-index.xml",
)

# Only 17.8% of live feeds are actually discoverable via <link rel=alternate>,
# so path probing adds real coverage over autodiscovery alone.
FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml", "/feeds/posts/default")

CMS_API_PATHS = (
    "/wp-json/wp/v2/posts?per_page=1",
    "/?rest_route=/wp/v2/posts&per_page=1",
    "/jsonapi/node/article",                    # Drupal
    "/blogs/news.atom",                         # Shopify
)

_ARTICLE_LD_TYPES = {"newsarticle", "article", "blogposting", "report",
                     "pressrelease", "reportagenewsarticle", "analysisnewsarticle"}


@dataclass
class DomainSurvey:
    """One row of the Phase 1 dataset."""

    newsroom_url: str
    domain: str
    ok: bool = False
    error: str | None = None

    # --- reachability -------------------------------------------------
    newsroom_status: int = 0
    newsroom_wall: str | None = None
    newsroom_bytes: int = 0
    elapsed_s: float = 0.0
    resolved_ip: str | None = None

    # --- robots -------------------------------------------------------
    robots_status: int = 0
    robots_allows: bool = True
    robots_crawl_delay: float | None = None
    robots_sitemaps: int = 0
    content_signals: dict = field(default_factory=dict)

    # --- discovery ----------------------------------------------------
    sitemap_url: str | None = None
    sitemap_source: str | None = None       # robots | probe
    has_news_sitemap: bool = False
    feed_url: str | None = None
    feed_source: str | None = None          # autodiscovery | probe
    feed_has_full_content: bool = False     # content:encoded present
    cms_generator: str | None = None
    cms_api_url: str | None = None
    wp_detected: bool = False
    wp_json_reachable: bool | None = None   # None => not a WP site
    wp_json_block_reason: str | None = None
    discovery_method: str | None = None     # the winner

    # --- article-level measurement ------------------------------------
    article_url: str | None = None
    article_status: int = 0
    article_links_found: int = 0
    has_jsonld: bool = False
    jsonld_types: list[str] = field(default_factory=list)
    jsonld_has_headline: bool = False
    jsonld_has_date: bool = False
    jsonld_has_articlebody: bool = False
    jsonld_articlebody_chars: int = 0
    text_chars: int = 0                     # trafilatura yield on plain HTML
    needs_browser: bool = False             # the JS-escalation signal
    needs_browser_reason: str | None = None
    hydration_payload: str | None = None    # __NEXT_DATA__ etc, if present


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _iter_jsonld(tree: LexborHTMLParser):
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    stack.extend(item["@graph"])
                yield item


def _ld_types(obj: dict) -> list[str]:
    t = obj.get("@type")
    if isinstance(t, str):
        return [t.lower()]
    if isinstance(t, list):
        return [str(x).lower() for x in t]
    return []


def _extract_text(html: str, url: str) -> int:
    """Characters of article text trafilatura can pull from raw HTML."""
    try:
        import trafilatura
        text = trafilatura.extract(
            html, url=url, include_comments=False, favor_precision=False,
        )
        return len(text or "")
    except Exception:
        return 0


def _detect_hydration(html: str) -> str | None:
    """SPA news pages very often ship the full body as JSON in the initial HTML.

    Finding one of these means a browser is probably NOT needed - the text is
    already here, it just needs mining out of the payload.
    """
    for marker in ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__",
                   "__APOLLO_STATE__", "__remixContext"):
        if marker in html:
            return marker
    return None


def _needs_browser(html: str, text_chars: int, has_body_in_ld: bool) -> tuple[bool, str | None]:
    """Escalate on OUTCOME, never on framework detection.

    Many Next.js/Nuxt news sites server-render perfectly well; routing to a
    browser because of an `id="__next"` marker would waste ~10x the cost for
    nothing.
    """
    if text_chars >= 200 or has_body_in_ld:
        return False, None
    if not html:
        return True, "empty response"
    lowered = html.lower()
    if "enable javascript" in lowered or "requires javascript" in lowered:
        return True, "noscript js-required notice"
    if len(html) < 1000:
        return True, "html under 1KB"
    script_count = lowered.count("<script")
    if script_count >= 3 and text_chars < 200:
        return True, f"low text ({text_chars} chars) with {script_count} scripts"
    return True, f"low text yield ({text_chars} chars)"


async def _resolve_ip(host: str, timeout: float) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP), timeout=timeout)
        return infos[0][4][0] if infos else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------

async def probe_domain(fetcher: PoliteFetcher, site: Site) -> DomainSurvey:
    s = DomainSurvey(newsroom_url=site.newsroom_url, domain=site.domain)
    origin = site.origin
    started = time.monotonic()

    s.resolved_ip = await _resolve_ip(site.host, fetcher.settings.politeness.dns_timeout_s)

    # --- robots -------------------------------------------------------
    rules = await fetcher.robots_for(site.newsroom_url)
    s.robots_status = rules.status
    s.robots_sitemaps = len(rules.sitemaps)
    s.content_signals = dict(rules.content_signals)
    ua = fetcher.settings.identity.user_agent
    s.robots_allows = rules.allows(site.newsroom_url, ua)
    s.robots_crawl_delay = rules.crawl_delay(ua)

    if not s.robots_allows:
        s.error = "robots-disallow"
        s.elapsed_s = time.monotonic() - started
        return s

    # --- the newsroom / listing page ----------------------------------
    resp = await fetcher.get(site.newsroom_url)
    s.newsroom_status = resp.status
    s.newsroom_wall = resp.wall
    s.newsroom_bytes = len(resp.text)
    if resp.error:
        s.error = resp.error
        s.elapsed_s = time.monotonic() - started
        return s

    tree = LexborHTMLParser(resp.text) if resp.text else None

    # --- CMS fingerprint ----------------------------------------------
    if tree is not None:
        gen = tree.css_first('meta[name="generator"]')
        if gen is not None:
            s.cms_generator = (gen.attributes.get("content") or "")[:120]
        s.wp_detected = ("/wp-content/" in resp.text or "/wp-includes/" in resp.text
                         or (s.cms_generator or "").lower().startswith("wordpress"))

    # --- feed autodiscovery -------------------------------------------
    if tree is not None:
        for node in tree.css('link[rel="alternate"]'):
            t = (node.attributes.get("type") or "").lower()
            if "rss" in t or "atom" in t or "xml" in t:
                href = node.attributes.get("href")
                if href:
                    s.feed_url = canonical_url(urljoin(site.newsroom_url, href))
                    s.feed_source = "autodiscovery"
                    break

    # --- sitemap from robots ------------------------------------------
    if rules.sitemaps:
        s.sitemap_url = rules.sitemaps[0]
        s.sitemap_source = "robots"

    # --- probe for what we did not find -------------------------------
    if not s.sitemap_url:
        for path in SITEMAP_PATHS:
            r = await fetcher.get(origin + path)
            if r.ok and ("<urlset" in r.text or "<sitemapindex" in r.text):
                s.sitemap_url = canonical_url(origin + path)
                s.sitemap_source = "probe"
                break

    if s.sitemap_url:
        r = await fetcher.get(s.sitemap_url)
        if r.ok:
            s.has_news_sitemap = "news.google.com/schemas/sitemap-news" in r.text or "<news:news" in r.text

    if not s.feed_url:
        for path in FEED_PATHS:
            r = await fetcher.get(origin + path)
            if r.ok and ("<rss" in r.text[:2000] or "<feed" in r.text[:2000]):
                s.feed_url = canonical_url(origin + path)
                s.feed_source = "probe"
                break

    if s.feed_url:
        r = await fetcher.get(s.feed_url)
        if r.ok:
            # A feed carrying content:encoded eliminates the article fetch
            # entirely - the politest and cheapest possible source.
            s.feed_has_full_content = "content:encoded" in r.text

    # --- CMS JSON API --------------------------------------------------
    if s.wp_detected:
        for path in CMS_API_PATHS[:2]:
            r = await fetcher.get(origin + path)
            if r.ok and r.text.lstrip().startswith("["):
                s.cms_api_url = canonical_url(origin + path)
                s.wp_json_reachable = True
                break
            if r.status in (401, 403) or "rest_cannot_access" in r.text or "rest_login_required" in r.text:
                s.wp_json_reachable = False
                s.wp_json_block_reason = f"status {r.status}"
        if s.wp_json_reachable is None:
            s.wp_json_reachable = False
            s.wp_json_block_reason = "no usable response"

    s.discovery_method = (
        "news_sitemap" if s.has_news_sitemap
        else "cms_api" if s.cms_api_url
        else "rss" if s.feed_url
        else "sitemap" if s.sitemap_url
        else "listing"
    )

    # --- pick an article and measure the real extraction path ----------
    if tree is not None:
        candidates: list[tuple[float, str]] = []
        seen: set[str] = set()
        for a in tree.css("a[href]"):
            href = a.attributes.get("href") or ""
            if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            absolute = canonical_url(urljoin(site.newsroom_url, href))
            if not absolute or absolute in seen:
                continue
            # Same registrable domain only - listing pages link out constantly.
            if registrable_domain(absolute) != site.domain:
                continue
            seen.add(absolute)
            verdict = classify_url(absolute)
            if verdict.is_article:
                candidates.append((verdict.score, absolute))
        s.article_links_found = len(candidates)
        candidates.sort(reverse=True)

        if candidates:
            s.article_url = candidates[0][1]
            ar = await fetcher.get(s.article_url)
            s.article_status = ar.status
            if ar.ok and ar.text:
                atree = LexborHTMLParser(ar.text)
                for obj in _iter_jsonld(atree):
                    types = _ld_types(obj)
                    if not types:
                        continue
                    s.jsonld_types = sorted(set(s.jsonld_types) | set(types))
                    if any(t in _ARTICLE_LD_TYPES for t in types):
                        s.has_jsonld = True
                        if obj.get("headline"):
                            s.jsonld_has_headline = True
                        if obj.get("datePublished"):
                            s.jsonld_has_date = True
                        body = obj.get("articleBody")
                        if isinstance(body, str) and body.strip():
                            s.jsonld_has_articlebody = True
                            s.jsonld_articlebody_chars = max(s.jsonld_articlebody_chars, len(body))

                s.text_chars = _extract_text(ar.text, s.article_url)
                s.hydration_payload = _detect_hydration(ar.text)
                s.needs_browser, s.needs_browser_reason = _needs_browser(
                    ar.text, s.text_chars, s.jsonld_has_articlebody)

    s.ok = s.error is None
    s.elapsed_s = time.monotonic() - started
    return s


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def read_sites(path: Path) -> list[Site]:
    """Read newsroom URLs from a .txt (one per line) or .csv (first URL column)."""
    sites: list[Site] = []
    seen: set[str] = set()
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        candidate = line
        if "," in line and not line.lower().startswith("http"):
            for cell in line.split(","):
                cell = cell.strip().strip('"')
                if cell.lower().startswith("http"):
                    candidate = cell
                    break
            else:
                continue
        candidate = candidate.split(",")[0].strip().strip('"')
        if not candidate.lower().startswith("http"):
            candidate = "https://" + candidate
        site = make_site(candidate)
        if not site.domain or site.domain in seen:
            continue
        seen.add(site.domain)
        sites.append(site)
    return sites


async def run_survey(
    sites: list[Site],
    out_path: Path,
    settings: Settings | None = None,
    concurrency: int = 16,
    progress=None,
) -> list[DomainSurvey]:
    settings = settings or Settings.load()
    results: list[DomainSurvey] = []
    sem = asyncio.Semaphore(concurrency)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # The file handle is a *sync* context manager, so it cannot join the
    # `async with` - nest it instead.
    with out_path.open("w", encoding="utf-8") as fh:
        async with PoliteFetcher(settings) as fetcher:
            lock = asyncio.Lock()

            async def one(site: Site) -> None:
                try:
                    async with sem:
                        result = await probe_domain(fetcher, site)
                except Exception as exc:                      # noqa: BLE001
                    result = DomainSurvey(newsroom_url=site.newsroom_url,
                                          domain=site.domain,
                                          error=f"{type(exc).__name__}: {exc}")
                async with lock:
                    results.append(result)
                    fh.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
                    fh.flush()      # crash-safe: partial surveys are still usable
                    if progress is not None:
                        progress(result)

            await asyncio.gather(*(one(s) for s in sites))

    return results


def summarize(results: list[DomainSurvey]) -> dict:
    """Turn the raw rows into the five numbers the plan is waiting on."""
    n = len(results)
    reachable = [r for r in results if r.ok and r.newsroom_status == 200]
    with_article = [r for r in reachable if r.article_status == 200]
    ips: dict[str, int] = {}
    for r in results:
        if r.resolved_ip:
            ips[r.resolved_ip] = ips.get(r.resolved_ip, 0) + 1
    shared = sum(c for c in ips.values() if c > 1)

    def pct(count: int, total: int) -> float:
        return round(100.0 * count / total, 1) if total else 0.0

    wp = [r for r in results if r.wp_detected]
    return {
        "domains_sampled": n,
        "reachable": pct(len(reachable), n),
        "cloudflare_walls": pct(sum(1 for r in results if r.newsroom_wall), n),
        "robots_disallow": pct(sum(1 for r in results if not r.robots_allows), n),
        "discovery": {
            "news_sitemap": pct(sum(1 for r in results if r.has_news_sitemap), n),
            "any_sitemap": pct(sum(1 for r in results if r.sitemap_url), n),
            "rss_autodiscovered": pct(sum(1 for r in results if r.feed_source == "autodiscovery"), n),
            "rss_via_probe": pct(sum(1 for r in results if r.feed_source == "probe"), n),
            "rss_full_content": pct(sum(1 for r in results if r.feed_has_full_content), n),
            "cms_api": pct(sum(1 for r in results if r.cms_api_url), n),
            "listing_only": pct(sum(1 for r in results if r.discovery_method == "listing"), n),
        },
        "wordpress": {
            "detected": pct(len(wp), n),
            "wp_json_reachable": pct(sum(1 for r in wp if r.wp_json_reachable), len(wp)),
        },
        "structured_data": {
            "article_pages_tested": len(with_article),
            "jsonld_article_type": pct(sum(1 for r in with_article if r.has_jsonld), len(with_article)),
            "has_headline": pct(sum(1 for r in with_article if r.jsonld_has_headline), len(with_article)),
            "has_datepublished": pct(sum(1 for r in with_article if r.jsonld_has_date), len(with_article)),
            "has_articlebody": pct(sum(1 for r in with_article if r.jsonld_has_articlebody), len(with_article)),
        },
        "js_escalation": {
            "needs_browser": pct(sum(1 for r in with_article if r.needs_browser), len(with_article)),
            "has_hydration_payload": pct(
                sum(1 for r in with_article if r.hydration_payload), len(with_article)),
            "median_text_chars": sorted(r.text_chars for r in with_article)[len(with_article) // 2]
            if with_article else 0,
        },
        "shared_ip": {
            "distinct_ips": len(ips),
            "domains_on_shared_ip": pct(shared, n),
            "largest_cluster": max(ips.values()) if ips else 0,
        },
    }
