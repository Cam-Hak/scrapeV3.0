"""The crawl loop: lease a domain, discover, extract, store, release.

One pass over the frontier. For each leased domain the worker crawls every
target on it *under that single lease*, so the 417 house.gov legislator pages
are paced as the one origin they actually are.

Ordering inside a target is deliberate and matches the cheapest-first rule:

    discover -> dedup check -> fetch -> extract -> store

The dedup check sits BEFORE the article fetch. That is the one piece of v2's
design worth preserving verbatim: checking first means an already-seen article
costs nothing, and on a daily re-crawl most articles are already seen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .discover.sources import ArticleRef, discover
from .extract import extract_article
from .extract.models import Article
from .extract.models import Path as ArticlePath
from .fetch import PoliteFetcher
from .frontier import Frontier, open_frontier
from .urls import canonical_url, classify_url, is_non_news_path, registrable_domain
from .settings import Settings
from .sink import Sink
from .tracing import get as _get_logger, slug, tag

log = _get_logger(__name__)

if TYPE_CHECKING:            # the TNS sink is optional; importing it is not
    from .tns import TnsSink


@dataclass
class CrawlStats:
    domains: int = 0
    targets: int = 0
    discovered: int = 0
    already_seen: int = 0
    fetched: int = 0
    stored: int = 0
    body_text_dupes: int = 0
    failed: int = 0
    needs_browser: int = 0
    unusable: int = 0
    off_domain: int = 0
    non_news: int = 0
    # Not a failure. robots.txt told us not to, and we did not - filing that
    # under "Why fetches failed" alongside 404s and timeouts is the same
    # mistake as filing a declined site-wide source under "errors": it makes
    # correct behaviour look like breakage and trains you to skim the list.
    robots_disallowed: int = 0
    # Articles the fetch reached but the window excluded. Counted because an
    # uncounted drop is indistinguishable from a bug: a run reporting 174
    # discovered, 36 stored and zero in every rejection row looks broken, and
    # the honest answer was "the rest were older than --max-age-days".
    too_old: int = 0
    # Agencies purged this pass because the shared removal list named them.
    removed_agencies: int = 0
    tns_loaded: int = 0
    tns_rejected: int = 0
    tns_failed: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    by_body_source: dict[str, int] = field(default_factory=dict)
    # Why fetches failed, tallied by reason. `failed: 15` with no breakdown is
    # not a diagnosis - one site 404ing every URL and fifteen sites timing out
    # once each are the same number and completely different problems.
    by_failure: dict[str, int] = field(default_factory=dict)
    # Which requirement an unusable article failed. Same reasoning as
    # by_failure: the count alone does not say what to go and look at.
    by_unusable: dict[str, int] = field(default_factory=dict)
    # Which domains produced each failure reason. A reason without attribution
    # is only half a diagnosis: "HTTP 404 x20" could be one site whose
    # discovery is building URLs that do not exist, or twenty sites each having
    # deleted one article, and those need completely different responses.
    failure_domains: dict[str, set[str]] = field(default_factory=dict)
    # And the same for unusable articles. "no headline x6" was undiagnosable
    # without it - six sites each losing one article and one site losing six
    # are different problems, and only the second is worth chasing.
    unusable_domains: dict[str, set[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # The cascade declining a source is a decision, not a failure. Kept apart
    # from `errors` so that list stays worth reading.
    notes: list[str] = field(default_factory=list)

    def bump(self, bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1


async def crawl_target(
    fetcher: PoliteFetcher,
    sink: Sink,
    *,
    domain: str,
    a_id: int,
    newsroom_url: str,
    known_feed: str | None,
    known_method: str | None,
    feed_absent: bool,
    max_articles: int,
    max_age_days: int,
    stats: CrawlStats,
    tns: "TnsSink | None" = None,
) -> tuple[str, str | None, bool]:
    """Crawl one newsroom URL. Returns (method, source_url, feed_absent)."""
    found = await discover(fetcher, newsroom_url,
                           known_feed=known_feed, known_method=known_method,
                           feed_absent=feed_absent, limit=max_articles * 3)
    stats.bump(stats.by_method, found.method)
    stats.discovered += len(found.articles)
    for err in found.errors:
        stats.errors.append(f"{domain}: {err}")
    for note in found.notes:
        stats.notes.append(f"{domain}: {note}")

    # Prefer articles under the target's own section. edisonohio.edu's target
    # is /News, but its site-wide feed also carries /about/edison-foundation/*
    # static pages. Scoping to the section is more principled than blocklisting
    # section names, and it falls back to everything if nothing matches.
    found.articles = _prefer_section(found.articles, newsroom_url)

    # Which publisher's content counts as this target's own. Normally the
    # frontier's domain, but a target whose newsroom page redirects to another
    # registrable domain has moved, and its articles legitimately live there -
    # dni.gov now serves from www.odni.gov. The frontier key stays put, since
    # that is the agency's identity for TNS; only the guard follows.
    publisher = found.target_domain or domain

    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    stored_here = 0
    stale_streak = 0
    seed = canonical_url(newsroom_url)

    for ref in found.articles:
        if stored_here >= max_articles:
            break

        # Never store the listing page itself. Sitemaps list section indexes
        # alongside articles, and battelle.org's own newsroom URL was stored
        # with the site's nav text as its body before this guard existed.
        if canonical_url(ref.url) == seed:
            log.debug("%s   - %-14s %s", tag(domain), "the seed page", slug(ref.url))
            continue

        # Only this publisher's own content. Plenty of organisations run
        # press-clipping feeds: ufw.org's RSS links to Politico, WBUR,
        # Courthouse News and the Yakima Herald. Without this guard those were
        # stored under ufw.org with UFW's agency id - attributing another
        # publisher's copyrighted article to the wrong source, which for a
        # newswire is a serious integrity problem, not a cosmetic one.
        if registrable_domain(ref.url) != publisher:
            stats.off_domain += 1
            log.debug("%s   - %-14s %s  (not %s)", tag(domain), "off-domain",
                      slug(ref.url), publisher)
            continue

        # Applies to EVERY source, feeds included. edisonohio.edu's /News feed
        # carried /event/2026-08/welcome-week - a campus event, not a press
        # release, and article-shaped enough to pass every other check.
        if is_non_news_path(ref.url):
            stats.non_news += 1
            log.debug("%s   - %-14s %s", tag(domain), "not news", slug(ref.url))
            continue

        # Feeds and CMS APIs only publish articles, so their URLs are trusted.
        # Sitemaps and harvested links are not - they contain section indexes,
        # tag pages and pagination, so those go through the classifier.
        if ref.source in ("sitemap", "listing"):
            verdict = classify_url(ref.url)
            if not verdict.is_article:
                log.debug("%s   - %-14s %s  (score %.2f)", tag(domain),
                          "not an article", slug(ref.url), verdict.score)
                continue

        # Cheap check first - before spending a request.
        if sink.seen_url(ref.url):
            stats.already_seen += 1
            log.debug("%s   - %-14s %s", tag(domain), "already have", slug(ref.url))
            # Feeds and sitemaps are reverse-chronological, so a run of
            # already-seen items means we have caught up. This is the crawl
            # budget optimisation v2 got right.
            stale_streak += 1
            if stale_streak >= 5:
                break
            continue
        stale_streak = 0

        article = await _extract_ref(fetcher, ref, stats, domain)
        if article is None:
            continue        # _extract_ref has already classified and counted it

        if article.quality.get("needs_browser"):
            stats.needs_browser += 1
        reason = article.unusable_reason
        if reason is not None:
            stats.unusable += 1
            stats.bump(stats.by_unusable, reason)
            stats.unusable_domains.setdefault(reason, set()).add(domain)
            log.debug("%s   - %-14s %s", tag(domain), reason[:14], slug(ref.url))
            continue

        if article.date.value and article.date.value < cutoff:
            stats.too_old += 1
            log.debug("%s   - %-14s %s  (%s)", tag(domain), "past window",
                      slug(ref.url), article.date.value.date())
            stale_streak += 1
            if stale_streak >= 5:
                break
            continue

        twin = sink.seen_content(article.body)
        if twin:
            # Same body under a different URL: a syndicated press release.
            # Worth recording as a cluster rather than silently dropping.
            stats.body_text_dupes += 1

        agency = tns.agencies.get(a_id) if tns is not None else None
        if sink.write(article, domain=domain, a_id=a_id,
                      agency_prefix=agency.prefix if agency else ""):
            stats.stored += 1
            stored_here += 1
            log.debug("%s   + %-14s %s  (%s chars, %s)", tag(domain), "stored",
                      slug(ref.url), f"{article.body_len:,}",
                      article.body_source.value)
            if tns is not None:
                load_to_tns(tns, sink, article, a_id=a_id, stats=stats)

    return found.method, found.feed_url, found.feed_absent


# What each TnsSink outcome means for whether the article should be offered
# again. Only "error" is retryable; a rejection is a verdict, not a hiccup.
_RETRYABLE = "insert_error"


def load_to_tns(tns: "TnsSink", sink: Sink, article: Article, *, a_id: int,
                stats: CrawlStats) -> str:
    """Offer a stored article to `tns.press_release` and record the outcome.

    Ordering matters: the JSONL row and the dedup index are written first,
    because the scrape is a fact, while the load is an action that can fail.
    Writing the load state back is what makes a failure retryable instead of
    permanently invisible - the dedup index would otherwise mark the article
    seen and it would never be offered again.
    """
    outcome = tns.load(
        a_id=a_id,
        headline=article.headline or "",
        body=article.body or "",
        published=article.date.value,
        url=article.url,
    )
    if tns.dry_run:
        # Nothing was written, so nothing may be recorded as written. Marking a
        # dry run "loaded" would make the real run's backfill skip it.
        stats.tns_loaded += 1 if outcome == "inserted" else 0
        return outcome

    if outcome == "inserted":
        sink.mark_tns(article.url, "loaded", tns.last_filename)
        stats.tns_loaded += 1
    elif outcome == _RETRYABLE:
        sink.mark_tns(article.url, "error")
        stats.tns_failed += 1
    else:
        sink.mark_tns(article.url, f"rejected:{outcome}")
        stats.tns_rejected += 1
    return outcome


def _prefer_section(refs, newsroom_url: str):
    """Keep refs under the newsroom URL's section path, if any qualify."""
    from urllib.parse import urlsplit

    section = urlsplit(canonical_url(newsroom_url)).path.rstrip("/").lower()
    # A one-segment section like /news is meaningful; the site root is not.
    if not section or section.count("/") < 1 or len(section) < 3:
        return refs
    in_section = [r for r in refs
                  if urlsplit(canonical_url(r.url)).path.lower().startswith(section + "/")]
    return in_section or refs


async def _extract_ref(fetcher: PoliteFetcher, ref: ArticleRef,
                       stats: CrawlStats, domain: str = "") -> Article | None:
    """Build an Article from a reference, fetching only when necessary."""
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # Route the date by WHERE it came from, because the two inputs rank very
    # differently in the cascade. A feed's pubDate and a wp-json date_gmt are
    # publisher-asserted and sit at the top; a sitemap's `lastmod` means
    # "significantly modified", not "published", and sits near the bottom.
    #
    # Passing both through `feed_date` promoted lastmod to the most trusted
    # signal there is. hccs.edu regenerated its sitemap and five articles from
    # March, April, May and August all arrived stamped 24 August - beating the
    # real date sitting in their own OpenGraph tags.
    publisher_date = (ref.date_raw
                      if ref.source in ("rss", "cms_api", "news_sitemap") else None)
    lastmod = ref.date_raw if ref.source == "sitemap" else None

    # A feed carrying content:encoded, or a wp-json post carrying rendered
    # content, already has the full body - no article fetch at all. This is
    # both the fastest path and the politest.
    if ref.has_full_body:
        article = extract_article(
            ref.body_html or "", ref.url,
            feed_headline=ref.headline, feed_date=publisher_date,
            sitemap_lastmod=lastmod, feed_body=None, fetched_at=fetched_at,
        )
        if article.usable:
            # trafilatura did the parsing, but the CONTENT came from the feed or
            # CMS payload. Credit the real origin - provenance drives the drift
            # monitoring, so "trafilatura" here would hide a source change.
            article.body_source = (
                ArticlePath.CMS_API if ref.source == "cms_api" else ArticlePath.FEED)
            article.quality["body_source"] = article.body_source.value
            article.quality["body_without_fetch"] = True
            stats.bump(stats.by_body_source, article.body_source.value)
            return article
        # Fall through and fetch the real page if the embedded body was a stub.

    resp = await fetcher.get(ref.url)
    stats.fetched += 1
    if not resp.ok:
        # A wall answers 200, so the status alone would report "HTTP 200" for
        # a page we never actually got.
        reason = resp.error or (f"bot wall: {resp.wall}" if resp.wall
                                else f"HTTP {resp.status}")
        if reason == "robots-disallow":
            stats.robots_disallowed += 1
            return None
        stats.failed += 1
        stats.bump(stats.by_failure, reason)
        if domain:
            stats.failure_domains.setdefault(reason, set()).add(domain)
        return None

    article = extract_article(
        resp.text, ref.url,
        feed_headline=ref.headline,
        feed_date=publisher_date,
        sitemap_lastmod=lastmod,
        http_last_modified=resp.headers.get("last-modified"),
        fetched_at=fetched_at,
    )
    stats.bump(stats.by_body_source, article.body_source.value)
    return article


def _apply_removals(settings: Settings, frontier: Frontier, sink: Sink,
                    tns: "TnsSink | None", stats: CrawlStats) -> int:
    """Purge every agency on the shared removal list. Never raises."""
    from . import removal

    try:
        conn = removal.connect(settings)
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"removal list unreachable: {type(exc).__name__}: {exc}")
        return 0
    try:
        removal.ensure_table(conn)
        reports = removal.reconcile(removal.listed(conn), frontier=frontier,
                                    sink=sink, tns=tns)
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"removal list failed: {type(exc).__name__}: {exc}")
        return 0
    finally:
        conn.close()

    for report in reports:
        stats.errors.extend(f"removing a_id {report.a_id}: {e}"
                            for e in report.errors)
    return len(reports)


async def crawl_once(
    *,
    settings: Settings | None = None,
    frontier: Frontier | None = None,
    sink: Sink | None = None,
    tns: "TnsSink | None" = None,
    domains: int = 10,
    only_domains: list[str] | None = None,
    only_a_id: int | None = None,
    max_articles: int = 10,
    max_age_days: int = 30,
    concurrency: int = 8,
    worker_id: str = "w1",
    progress=None,
) -> CrawlStats:
    """Lease and crawl a batch of domains."""
    settings = settings or Settings.load()
    owns_frontier = frontier is None
    owns_sink = sink is None
    frontier = frontier or open_frontier()
    sink = sink or Sink(settings.data_dir)
    stats = CrawlStats()

    try:
        frontier.release_expired_leases()

        # Before anything is leased, so a removal takes effect this pass rather
        # than the next one. An unreachable list is logged and stepped over: a
        # dashboard being down is an intake problem, not a reason to stop
        # collecting news, and the list is reconciled again next pass anyway.
        if settings.removal_enabled:
            stats.removed_agencies = _apply_removals(settings, frontier, sink,
                                                     tns, stats)
        # A named scope bypasses the due-queue; the schedule is what is being
        # overridden, never the lease or the per-host pacing.
        leased = (frontier.acquire_domains(worker_id, only_domains) if only_domains
                  else frontier.acquire(worker_id, limit=domains))
        stats.domains = len(leased)
        if not leased:
            return stats

        sem = asyncio.Semaphore(concurrency)

        async with PoliteFetcher(settings) as fetcher:

            async def one_domain(record) -> None:
                async with sem:
                    method, feed_url, feed_absent = "none", None, False
                    ok = False
                    try:
                        # Every target on this domain, under the one lease -
                        # unless one agency was asked for by name, in which case
                        # only its own newsroom URLs. house.gov carries 417.
                        targets = record.targets or []
                        if only_a_id is not None:
                            targets = [t for t in targets if t.a_id == only_a_id]
                        for target in targets:
                            stats.targets += 1
                            method, feed_url, feed_absent = await crawl_target(
                                fetcher, sink,
                                domain=record.domain,
                                a_id=target.a_id,
                                newsroom_url=target.newsroom_url,
                                known_feed=target.feed_url,
                                known_method=target.discovery_method,
                                # Honour the cached verdict only while fresh.
                                feed_absent=target.feed_absence_is_fresh(),
                                max_articles=max_articles,
                                max_age_days=max_age_days,
                                stats=stats,
                                tns=tns,
                            )
                            frontier.release_target(
                                target.newsroom_url,
                                success=method != "none",
                                discovery_method=method if method != "none" else None,
                                feed_url=feed_url,
                                # Only write the verdict when we actually probed.
                                feed_absent=True if feed_absent else None,
                            )
                            ok = ok or method != "none"
                    except Exception as exc:                   # noqa: BLE001
                        stats.errors.append(f"{record.domain}: {type(exc).__name__}: {exc}")
                    finally:
                        frontier.release(
                            record.domain,
                            success=ok,
                            discovery_method=method if method != "none" else None,
                            feed_url=feed_url,
                        )
                        if progress is not None:
                            progress(record.domain)

            await asyncio.gather(*(one_domain(r) for r in leased))
    finally:
        if owns_sink:
            sink.close()
        if owns_frontier:
            frontier.close()

    return stats
