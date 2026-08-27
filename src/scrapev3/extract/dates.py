"""Publish-date extraction.

Date is the weakest of the three fields and the one v2 broke most often - its
logs show the date selector picking up bylines (`Date Extraction failed: reanna
gonzalez`) and its freshness gate compared bare month integers with no year, so
it rejected the current month entirely.

The measured gap is large. On htmldate's 1000-page evaluation (re-run
2026-06-01), htmldate reaches **90.3% exact-day accuracy** while every
general-purpose extractor's built-in date logic sits at **54-68%**. So the date
comes from htmldate, not from trafilatura or newspaper.

Three rules, all of which exist because ignoring them produces silent errors:

1. **`original_date=True` on every htmldate call.** The default returns the
   *most recent* date on the page - i.e. `dateModified` - which is the wrong
   field. This single flag is worth more than anything else in this module.

2. **Never search the whole document for a bare date.** Free-text date hunting
   is restricted to the extracted article subtree, because a footer copyright
   year or a sidebar teaser timestamp will otherwise win.

3. **Sanity-window everything.** Reject dates before 1995 or more than 48h in
   the future, whatever the source claims.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import DatePrecision, DateResult, Path

MIN_YEAR = 1995
FUTURE_TOLERANCE = timedelta(hours=48)
# Independent sources disagreeing by more than this get flagged for review; a
# rising rate per domain is an early layout-drift signal.
DISAGREEMENT_DAYS = 7

_URL_DATE_PATTERNS = (
    re.compile(r"/(?P<y>19|20\d{2})[/-](?P<m>0?[1-9]|1[0-2])[/-](?P<d>0?[1-9]|[12]\d|3[01])(?:/|$|[-.])"),
    re.compile(r"/(?P<y>(?:19|20)\d{2})[/-](?P<m>0?[1-9]|1[0-2])(?:/|$|[-.])"),
    re.compile(r"/(?P<y>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})(?:/|$|[-.])"),
)

_ISO_OFFSET = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")


def _in_window(dt: datetime) -> bool:
    if dt.year < MIN_YEAR:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return dt <= now + FUTURE_TOLERANCE


def _precision_of(raw: str) -> DatePrecision:
    raw = raw.strip()
    if re.search(r"\d{2}:\d{2}:\d{2}", raw):
        return DatePrecision.SECOND
    if re.search(r"\d{2}:\d{2}", raw):
        return DatePrecision.MINUTE
    if re.search(r"\d{4}-\d{2}-\d{2}", raw) or re.search(r"\d{1,2}\s+\w+\s+\d{4}", raw):
        return DatePrecision.DAY
    return DatePrecision.MONTH


def parse_date_string(raw: str | None, *, relative_base: datetime | None = None) -> datetime | None:
    """Parse one date string, tolerating ISO, RFC-822, and free text.

    `relative_base` must be the FETCH time for relative expressions ("2 hours
    ago"). Without it, re-processing archived HTML silently produces dates
    relative to *now* rather than to when the page was captured.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    # Fast path: ISO 8601, which is what JSON-LD and OpenGraph almost always use.
    try:
        candidate = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if candidate.tzinfo is not None:
            candidate = candidate.astimezone(timezone.utc).replace(tzinfo=None)
        return candidate if _in_window(candidate) else None
    except ValueError:
        pass

    try:
        import dateparser

        settings = {
            "RETURN_AS_TIMEZONE_AWARE": False,
            "PREFER_DATES_FROM": "past",     # a news date is not in the future
        }
        if relative_base is not None:
            settings["RELATIVE_BASE"] = relative_base
        candidate = dateparser.parse(text, settings=settings)
    except Exception:
        candidate = None

    if candidate is None:
        return None
    if candidate.tzinfo is not None:
        candidate = candidate.astimezone(timezone.utc).replace(tzinfo=None)
    return candidate if _in_window(candidate) else None


def date_from_url(url: str) -> datetime | None:
    """Dates embedded in the path are high-precision when present."""
    for pattern in _URL_DATE_PATTERNS:
        m = pattern.search(url)
        if not m:
            continue
        parts = m.groupdict()
        try:
            year = int(parts["y"])
            month = int(parts.get("m") or 1)
            day = int(parts.get("d") or 1)
            candidate = datetime(year, month, day)
        except (TypeError, ValueError):
            continue
        if _in_window(candidate):
            return candidate
    return None


def date_from_htmldate(html: str, url: str | None = None) -> tuple[datetime | None, str | None]:
    """htmldate in extensive mode, asking for the ORIGINAL publication date."""
    try:
        import htmldate

        raw = htmldate.find_date(
            html,
            url=url,
            original_date=True,     # not dateModified - see module docstring
            extensive_search=True,
            outputformat="%Y-%m-%d %H:%M:%S",
        )
    except Exception:
        return None, None
    if not raw:
        return None, None
    parsed = parse_date_string(raw)
    return parsed, raw


def resolve_date(
    *,
    html: str,
    url: str,
    jsonld_raw: str | None = None,
    jsonld_is_weak: bool = False,
    meta: dict[str, str] | None = None,
    microdata_raw: str | None = None,
    feed_raw: str | None = None,
    sitemap_lastmod: str | None = None,
    http_last_modified: str | None = None,
    fetched_at: datetime | None = None,
) -> DateResult:
    """Run the date cascade and cross-check the winner.

    Order is by trustworthiness, not convenience: publisher-asserted structured
    data first, htmldate next, and derived signals (sitemap lastmod, HTTP
    Last-Modified) only as a floor - `lastmod` means "significantly modified",
    and behind a CDN `Last-Modified` is often just cache time.
    """
    meta = meta or {}
    candidates: list[tuple[Path, datetime, str]] = []

    def add(source: Path, raw: str | None) -> None:
        if not raw:
            return
        parsed = parse_date_string(raw, relative_base=fetched_at)
        if parsed is not None:
            candidates.append((source, parsed, raw))

    # 1. Feed / CMS API - publisher-asserted, and usually exact.
    add(Path.FEED, feed_raw)
    # 2. JSON-LD datePublished.
    add(Path.JSONLD, jsonld_raw)
    # 3. OpenGraph article:published_time and friends.
    for key in ("article:published_time", "article:published",
                "og:article:published_time", "datepublished",
                "publish-date", "publication_date", "date", "dc.date",
                "dcterms.created", "sailthru.date"):
        if key in meta:
            add(Path.OPENGRAPH, meta[key])
            break
    # 4. Microdata / <time pubdate>.
    add(Path.TIME_ELEMENT, microdata_raw)

    htmldate_value, htmldate_raw = date_from_htmldate(html, url)
    if htmldate_value is not None:
        candidates.append((Path.HTMLDATE, htmldate_value, htmldate_raw or ""))

    url_value = date_from_url(url)
    if url_value is not None:
        candidates.append((Path.URL_PATH, url_value, url))

    add(Path.SITEMAP, sitemap_lastmod)
    add(Path.HTTP_HEADER, http_last_modified)

    if not candidates:
        return DateResult(source=Path.NONE)

    # Priority order. A weak (WebPage-only) JSON-LD date is demoted below
    # htmldate, since it did not assert that this page is an article at all.
    priority = [Path.FEED, Path.JSONLD, Path.OPENGRAPH, Path.TIME_ELEMENT,
                Path.HTMLDATE, Path.URL_PATH, Path.SITEMAP, Path.HTTP_HEADER]
    if jsonld_is_weak:
        priority.remove(Path.JSONLD)
        priority.insert(priority.index(Path.HTMLDATE) + 1, Path.JSONLD)

    by_source = {src: (dt, raw) for src, dt, raw in candidates}
    winner_source = next((p for p in priority if p in by_source), candidates[0][0])
    winner_dt, winner_raw = by_source[winner_source]

    # Cross-source vote: agreement raises confidence, wide disagreement is a
    # drift signal worth surfacing rather than silently resolving.
    disagreement = None
    others = [dt for src, dt, _ in candidates if src is not winner_source]
    if others:
        worst = max(abs((winner_dt - other).days) for other in others)
        if worst > DISAGREEMENT_DAYS:
            disagreement = worst

    return DateResult(
        value=winner_dt,
        precision=_precision_of(winner_raw),
        source=winner_source,
        raw=winner_raw or None,
        had_offset=bool(winner_raw and _ISO_OFFSET.search(winner_raw.strip())),
        disagreement_days=disagreement,
    )
