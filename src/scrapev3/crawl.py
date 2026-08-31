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
from .fetch import PoliteFetcher, failure_kind
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
    # Sources that answered 304. Counted because an armed run and an unarmed
    # run both reporting `discovered: 0` must not look identical - the same
    # argument `too_old` already makes.
    not_modified: int = 0
    # Agencies purged this pass because the shared removal list named them.
    removed_agencies: int = 0
    # Targets seeded this pass because the shared request list named them.
    requested_sites: int = 0
    # Requests refused because the agency is also on the removal list. Counted
    # rather than logged and forgotten: it means the website is asking for two
    # incompatible things, and nobody finds that out from a quiet skip.
    refused_requests: int = 0
    # Agency rows written to the website's status grid, when publishing is on.
    status_published: int = 0
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

    # One example of the message each kind was derived from. `by_failure` is
    # keyed on the kind now, which is what stops it fragmenting - but "dns x20"
    # with no example is a summary nobody can act on, so one is kept.
    failure_sample: dict[str, str] = field(default_factory=dict)
    # Faults as (kind, domain) -> [occurrences, a_id, url, detail], ready for
    # `FaultStore.record`. Accumulated here rather than written per occurrence
    # so a crawl never waits on a disk write inside the per-article loop, and so
    # a run that dies mid-pass still persists whatever the `finally` reaches.
    faults: dict[tuple[str, str], list] = field(default_factory=dict)

    def bump(self, bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def record_fault(self, kind: str, domain: str, *, a_id: int | None = None,
                     url: str | None = None, detail: str | None = None) -> None:
        """Note one occurrence. First writer supplies the sample."""
        entry = self.faults.get((kind, domain))
        if entry is None:
            self.faults[(kind, domain)] = [1, a_id, url, detail]
        else:
            entry[0] += 1

    def to_dict(self) -> dict:
        """The counters, JSON-safe, for `fault_run.stats_json`.

        The sets in `failure_domains`/`unusable_domains` become sorted lists;
        everything else is already a number or a string. This is the
        serialisation `CrawlStats` never had - the reason a run's diagnosis
        died with the process.
        """
        from dataclasses import asdict

        out = {}
        for key, value in asdict(self).items():
            if key == "faults":
                continue                      # persisted as rows, not as JSON
            if isinstance(value, dict) and value and all(
                    isinstance(v, set) for v in value.values()):
                out[key] = {k: sorted(v) for k, v in value.items()}
            else:
                out[key] = value
        return out


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
    known_etag: str | None = None,
    known_last_modified: str | None = None,
) -> tuple[str, str | None, bool, "Discovery"]:
    """Crawl one newsroom URL. Returns (method, source_url, feed_absent, found)."""
    found = await discover(fetcher, newsroom_url,
                           known_feed=known_feed, known_method=known_method,
                           feed_absent=feed_absent,
                           known_etag=known_etag,
                           known_last_modified=known_last_modified,
                           limit=max_articles * 3)
    # Nothing has changed since we last looked. The cheapest possible outcome,
    # and emphatically not a failure - see `Discovery.not_modified`.
    if found.not_modified:
        stats.not_modified += 1
        log.debug("%s unchanged since last run (304)", tag(domain))
        return known_method or found.method, known_feed, feed_absent, found
    stats.bump(stats.by_method, found.method)
    stats.discovered += len(found.articles)
    for err in found.errors:
        stats.errors.append(f"{domain}: {err}")
        # One bucket rather than eight, on purpose. The cascade has eight ways
        # to end at `method="none"` and telling them apart means mapping error
        # prose to codes - a fourth vocabulary that drifts the first time a
        # message is reworded. The domain, the count and the sample sentence are
        # what make it actionable, and they are exact.
        if domain:
            stats.record_fault("discover_failed", domain, a_id=a_id,
                               url=newsroom_url, detail=err)
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
    # How many of this target's articles came back as JS shells. A majority is
    # the signal that a browser would genuinely help here.
    extracted_here = 0
    js_shells = 0
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

        article = await _extract_ref(fetcher, ref, stats, domain, a_id)
        if article is None:
            continue        # _extract_ref has already classified and counted it

        extracted_here += 1
        if article.quality.get("needs_browser"):
            stats.needs_browser += 1
            js_shells += 1
        reason = article.unusable_reason
        if reason is not None:
            stats.unusable += 1
            stats.bump(stats.by_unusable, reason)
            if domain:
                stats.record_fault(article.unusable_code, domain, a_id=a_id,
                                   url=article.url, detail=reason)
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

    # A clear majority of shells, on a site that never refused us, is
    # `js_rendered`: fixable by rendering, with no arms race involved.
    if extracted_here >= 3 and js_shells / extracted_here > 0.6:
        found.js_rendered = True
    return found.method, found.feed_url, found.feed_absent, found


# What each TnsSink outcome means for whether the article should be offered
# again. Only "error" is retryable; a rejection is a verdict, not a hiccup.
_RETRYABLE = "insert_error"

# Above this share of a pass's hostnames failing to resolve, the fault is ours
# rather than the publishers'. Deliberately not 100%: a handful of genuinely
# dead hosts is normal in a 50k corpus, and waiting for every single one to
# fail would never fire.
_RESOLVER_ALARM = 0.25

# Faults in our own machinery are not about a publisher, so they are filed
# under one pseudo-domain rather than blamed on whichever site was in flight.
# It keeps `--domain` honest and stops an unreachable removal list from making
# a working publisher look broken.
_CRAWLER = "(crawler)"


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
                       stats: CrawlStats, domain: str = "",
                       a_id: int | None = None) -> Article | None:
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
        # `failure_kind` already handles the case the old inline expression was
        # written for - a wall answers 200, so the status alone would report
        # "HTTP 200" for a page we never actually got - and it handles the four
        # unrelated things `status = 0` means, which the inline version did not.
        #
        # Keyed on the kind rather than on `resp.error`, which is
        # f"{ClassName}: {message}" with the URL inside it: two timeouts on
        # different URLs used to be two rows, so the histogram fragmented into
        # exactly the per-article noise it exists to summarise.
        kind = failure_kind(resp)
        if kind == "robots":
            stats.robots_disallowed += 1
            return None
        detail = resp.error or (f"bot wall: {resp.wall}" if resp.wall
                                else f"HTTP {resp.status}")
        stats.failed += 1
        stats.bump(stats.by_failure, kind)
        # The message the kind was derived from, kept once per kind so the
        # detail survives the summarising.
        stats.failure_sample.setdefault(kind, detail)
        if domain:
            stats.failure_domains.setdefault(kind, set()).add(domain)
            stats.record_fault(kind, domain, a_id=a_id, url=ref.url,
                               detail=detail)
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
        stats.record_fault("admin_list_unreachable", _CRAWLER,
                           detail=f"removal list unreachable: {type(exc).__name__}: {exc}")
        return 0
    try:
        removal.ensure_table(conn)
        reports = removal.reconcile(removal.listed(conn), frontier=frontier,
                                    sink=sink, tns=tns)
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"removal list failed: {type(exc).__name__}: {exc}")
        stats.record_fault("admin_list_failed", _CRAWLER,
                           detail=f"removal list failed: {type(exc).__name__}: {exc}")
        return 0
    finally:
        conn.close()

    for report in reports:
        stats.errors.extend(f"removing a_id {report.a_id}: {e}"
                            for e in report.errors)
    return len(reports)


def _apply_requests(settings: Settings, frontier: Frontier,
                    stats: CrawlStats) -> tuple[int, int]:
    """Seed every site on the shared request list. Never raises.

    Returns (targets seeded, requests refused). The removal list is read here
    too and wins: an agency on both must not be seeded, or a request would
    quietly undo a removal a publisher asked for on every single pass.
    """
    from . import removal, site_requests

    try:
        conn = site_requests.connect(settings)
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"request list unreachable: {type(exc).__name__}: {exc}")
        stats.record_fault("admin_list_unreachable", _CRAWLER,
                           detail=f"request list unreachable: {type(exc).__name__}: {exc}")
        return 0, 0
    try:
        site_requests.ensure_table(conn)
        removal.ensure_table(conn)
        report = site_requests.reconcile(site_requests.listed(conn),
                                         frontier=frontier,
                                         removed=removal.listed(conn))
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"request list failed: {type(exc).__name__}: {exc}")
        stats.record_fault("admin_list_failed", _CRAWLER,
                           detail=f"request list failed: {type(exc).__name__}: {exc}")
        return 0, 0
    finally:
        conn.close()

    stats.errors.extend(f"requested site has no usable domain: {u}"
                        for u in report.invalid)
    return report.seeded, len(report.refused)


def _persist_faults(settings: Settings, stats: CrawlStats, run_id: str,
                    scope: str | None) -> int:
    """Write this pass's faults to the store. Never raises.

    Swallowed for the same reason `_publish_status` is: a diagnostic failing to
    record is not a reason to lose the crawl that produced it. The failure goes
    into `stats.errors`, which is printed.
    """
    from .faults import FaultStore

    try:
        store = FaultStore(settings.data_dir)
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"faults not recorded: {type(exc).__name__}: {exc}")
        return 0
    try:
        store.start_run(run_id, command="crawl", scope=scope)
        for (kind, domain), (n, a_id, url, detail) in stats.faults.items():
            store.record(run_id, kind, domain, n=n, a_id=a_id, url=url,
                         detail=detail)
        store.finish_run(run_id, domains=stats.domains, targets=stats.targets,
                         stats=stats.to_dict())
        store.prune()
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"faults not recorded: {type(exc).__name__}: {exc}")
        return 0
    finally:
        store.close()
    return len(stats.faults)


def _publish_status(settings: Settings, frontier: Frontier, sink: Sink,
                    stats: CrawlStats) -> int:
    """Publish per-agency health for the website. Never raises.

    Runs at the END of a pass, on the state the pass just produced, so the grid
    a publisher refreshes reflects the crawl that has actually finished rather
    than the one about to start.

    The whole grid is rewritten, not only the domains this pass leased: health
    is time-dependent - a site goes stale by the clock, without anything
    happening to it - so a row nobody visited this pass still has to be
    recomputed or it stays green forever.
    """
    from . import status as status_mod

    try:
        rows = status_mod.compose(frontier, sink)
        conn = status_mod.connect(settings)
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"status not published: {type(exc).__name__}: {exc}")
        stats.record_fault("admin_status_unpublished", _CRAWLER,
                           detail=f"status not published: {type(exc).__name__}: {exc}")
        return 0
    try:
        status_mod.ensure_table(conn)
        written = status_mod.publish(conn, rows)
        status_mod.prune(conn, [r.a_id for r in rows])
        return written
    except Exception as exc:                                # noqa: BLE001
        stats.errors.append(f"status not published: {type(exc).__name__}: {exc}")
        stats.record_fault("admin_status_unpublished", _CRAWLER,
                           detail=f"status not published: {type(exc).__name__}: {exc}")
        return 0
    finally:
        conn.close()


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

    # Stamped before the work, so the run is identifiable even if the pass dies
    # and only the `finally` gets to write. Same shape as `data/audits/`, so the
    # two sort together.
    from .faults import new_run_id

    run_id = new_run_id()
    scope = (", ".join(only_domains) if only_domains
             else f"a_id {only_a_id}" if only_a_id else None)

    try:
        frontier.release_expired_leases()

        # Before anything is leased, so a removal takes effect this pass rather
        # than the next one. An unreachable list is logged and stepped over: a
        # dashboard being down is an intake problem, not a reason to stop
        # collecting news, and the list is reconciled again next pass anyway.
        # Requests first, removals second, so a same-pass conflict resolves to
        # removed. `reconcile` already refuses a request whose agency is on the
        # removal list; running the purge afterwards as well means the order
        # holds even if the two lists changed between the two reads.
        if settings.requests_enabled:
            stats.requested_sites, stats.refused_requests = _apply_requests(
                settings, frontier, stats)
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

        # What each leased domain did to us LAST time, so the browser tier can
        # be pointed only at hosts that have already earned it. Built here
        # because the frontier is in hand here; the fetcher never reads it, and
        # a stale verdict is ignored - sites remove challenges, and a permanent
        # verdict would never notice.
        escalate = {r.domain: r.access for r in leased
                    if r.access and r.access_verdict_is_fresh()}

        async with PoliteFetcher(settings, escalate=escalate) as fetcher:

            async def one_domain(record) -> None:
                async with sem:
                    method, feed_url, feed_absent = "none", None, False
                    js_rendered = False
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
                            (method, feed_url, feed_absent,
                             found) = await crawl_target(
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
                                known_etag=target.etag,
                                known_last_modified=target.last_modified,
                            )
                            # A 304 is a reached origin, so it counts as
                            # success. Without this the politest possible
                            # outcome would drive the failure counter and the
                            # website would report a quiet publisher as broken.
                            js_rendered = js_rendered or found.js_rendered
                            reached = method != "none" or found.not_modified
                            frontier.release_target(
                                target.newsroom_url,
                                success=reached,
                                discovery_method=method if method != "none" else None,
                                feed_url=feed_url,
                                etag=found.etag,
                                last_modified=found.last_modified,
                                # Only write the verdict when we actually probed.
                                feed_absent=True if feed_absent else None,
                            )
                            ok = ok or reached
                    except Exception as exc:                   # noqa: BLE001
                        stats.errors.append(f"{record.domain}: {type(exc).__name__}: {exc}")
                        # The one that must never be silent. Everything else
                        # here is a classified failure; this is the crawl
                        # raising where nothing anticipated it, and it used to
                        # be one of eight strings truncated to 110 characters.
                        stats.record_fault("admin_target_crashed", record.domain,
                                           a_id=record.a_id,
                                           url=record.newsroom_url,
                                           detail=f"{type(exc).__name__}: {exc}")
                    finally:
                        # What refused us, remembered. The in-process counter
                        # dies with the fetcher and the frontier's own counter
                        # never learned WHY, so `needs_browser` sat wired all
                        # the way through the status table to the website with
                        # nothing writing it. `challenge` is the only verdict a
                        # browser could help with - of 41 walls in the first
                        # corpus run, 30 were flat denials that render exactly
                        # the same in Chrome, and claiming those "need a
                        # browser" would be a false statement on 30 rows.
                        verdict = fetcher.host_verdict(record.domain)
                        # A site that never refused us but serves JS shells is
                        # `js_rendered`, and that is the only verdict here a
                        # browser fixes without any arms race. A refusal always
                        # outranks it: it is the harder fact about the host.
                        access = verdict.access if verdict else None
                        if access is None and js_rendered:
                            access = "js_rendered"
                        frontier.release(
                            record.domain,
                            success=ok,
                            discovery_method=method if method != "none" else None,
                            feed_url=feed_url,
                            access=access,
                            needs_browser=(access in ("challenge", "js_rendered")
                                           if access else None),
                        )
                        if progress is not None:
                            progress(record.domain)

            await asyncio.gather(*(one_domain(r) for r in leased))

            # One loud line about our own resolver, rather than one quiet
            # "site unreachable" per publisher. A resolver broken for a whole
            # TLD reported 20 .mil agencies as failing sites through a full
            # corpus run, and nothing in the output pointed at the resolver.
            attempted, failed = fetcher.resolver_report()
            if attempted and failed / attempted > _RESOLVER_ALARM:
                stats.record_fault("admin_resolver_failing", _CRAWLER,
                                   n=failed,
                                   detail=f"{failed} of {attempted} hostnames")
                stats.errors.append(
                    f"local DNS resolver failed for {failed} of {attempted} "
                    f"hostnames - these are not site failures. Check the "
                    f"resolver, or set SCRAPEV3_DOH_URL")
    finally:
        # In `finally`, not after the gather, so it also runs on the two paths
        # that skip the crawl body: nothing due to lease, and an exception. The
        # grid ages by the clock rather than by what was crawled, so a pass that
        # leased nothing still has staleness to report.
        if settings.status_enabled:
            stats.status_published = _publish_status(settings, frontier, sink, stats)
        if settings.faults_enabled:
            _persist_faults(settings, stats, run_id, scope)
        if owns_sink:
            sink.close()
        if owns_frontier:
            frontier.close()

    return stats
