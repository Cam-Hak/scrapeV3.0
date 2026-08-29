"""Per-agency health, published for the website to render.

The mirror image of `removal.py`. That table is written by the website and read
by the crawler; this one is written by the crawler and read by the website. Same
integration surface either way - one table in the shared `scrapev3` schema, no
API, no socket, no shared filesystem, no shared Python environment.

**The crawler decides health; the website only draws it.** Putting the rules in
PHP would mean two definitions of "healthy" - one in the code that knows what a
consecutive failure or a browser escalation actually costs, and one in a
template - and they would drift without anyone noticing. So a row carries a
`health` word, a `severity` for colouring, and a `reason` in plain English, and
the grid is a lookup from severity to a colour.

`severity` is a deliberately tiny closed vocabulary (`ok` / `warn` / `error`)
precisely so it can be relied on: adding a new `health` word later - a new way a
site can be unwell - must not require touching the website to keep it rendering.

**"No news" is not "broken".** The distinction the crawler can make and a naive
grid cannot: a site we fetch successfully that simply has not published in three
months is `quiet`, not `failing`. Conflating the two produces a dashboard that
cries wolf on every low-volume publisher, which is most of them - so `quiet` is
`ok`, and only the fetch actually failing is `error`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

from .settings import Settings
from .tracing import get as _get_logger

if TYPE_CHECKING:
    from .frontier import Frontier
    from .sink import Sink

log = _get_logger(__name__)

# Consecutive failures before an agency is called failing rather than unlucky.
# The frontier doubles its backoff per failure (`backoff_seconds`), so by the
# third the site has been retried across an interval wide enough that a blip
# would have cleared. Below this a single timeout would light the grid red.
FAILING_AFTER = 3

# A successful crawl older than this means the schedule has stopped reaching
# the site, whatever the failure counter says. Two weeks, against a default
# revisit period of one day, so a site has to miss many passes to qualify.
STALE_AFTER_DAYS = 14

# Fetching fine, but the publisher has posted nothing. Institutional newsrooms
# routinely go a month between releases, so this is set well past that: it
# flags "this feed may have moved" without flagging every quiet quarter.
QUIET_AFTER_DAYS = 90

# The window "articles_recent" counts, so the grid can show volume rather than
# only a total that never goes down.
RECENT_DAYS = 30

# ok    - working, or working and simply quiet
# warn  - reaching it, but not getting what we want
# error - not getting anything
_SEVERITY = {
    "healthy": "ok",
    "quiet": "ok",
    "disabled": "warn",
    "stale": "warn",
    "blocked": "warn",
    "empty": "warn",
    "never": "error",
    "failing": "error",
}

_DDL = """
CREATE TABLE IF NOT EXISTS agency_status (
  a_id              INT NOT NULL,
  domain            VARCHAR(255) NOT NULL,
  newsroom_url      TEXT NULL,
  enabled           TINYINT(1) NOT NULL DEFAULT 1,
  health            VARCHAR(16) NOT NULL,
  severity          VARCHAR(8) NOT NULL,
  reason            VARCHAR(255) NULL,
  discovery_method  VARCHAR(32) NULL,
  targets           SMALLINT NOT NULL DEFAULT 1,
  consec_failures   SMALLINT NOT NULL DEFAULT 0,
  needs_browser     TINYINT(1) NOT NULL DEFAULT 0,
  articles          INT NOT NULL DEFAULT 0,
  articles_recent   INT NOT NULL DEFAULT 0,
  median_body_len   INT NULL,
  last_success_at   DATETIME NULL,
  last_article_at   DATETIME NULL,
  updated_at        DATETIME NOT NULL,
  PRIMARY KEY (a_id),
  KEY idx_status_severity (severity),
  KEY idx_status_domain (domain)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Written and read by name, never by position: the website selects columns
# explicitly, so a column added here cannot shift the meaning of another.
COLUMNS = (
    "a_id", "domain", "newsroom_url", "enabled", "health", "severity", "reason",
    "discovery_method", "targets", "consec_failures", "needs_browser",
    "articles", "articles_recent", "median_body_len", "last_success_at",
    "last_article_at", "updated_at",
)


@dataclass
class AgencyStatus:
    """One row of the grid: what we know about one agency's crawl."""

    a_id: int
    domain: str
    newsroom_url: str | None = None
    enabled: bool = True
    health: str = "never"
    severity: str = "error"
    reason: str | None = None
    discovery_method: str | None = None
    targets: int = 1
    consec_failures: int = 0
    needs_browser: bool = False
    articles: int = 0
    articles_recent: int = 0
    median_body_len: int | None = None
    last_success_at: datetime | None = None
    last_article_at: datetime | None = None
    updated_at: datetime | None = None

    def as_row(self) -> tuple:
        """Values in `COLUMNS` order, for the upsert."""
        d = asdict(self)
        d["enabled"] = int(self.enabled)
        d["needs_browser"] = int(self.needs_browser)
        return tuple(d[c] for c in COLUMNS)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, for `--json` and the demo page's fixture."""
        d = asdict(self)
        for key in ("last_success_at", "last_article_at", "updated_at"):
            value = d[key]
            d[key] = value.isoformat(sep=" ", timespec="seconds") if value else None
        return d


# ---------------------------------------------------------------------------
# Composing it
# ---------------------------------------------------------------------------

def classify(*, enabled: bool, consec_failures: int, needs_browser: bool,
             last_success_at: datetime | None, last_article_at: datetime | None,
             articles: int, now: datetime) -> tuple[str, str]:
    """Health and the sentence explaining it.

    Ordered most-conclusive first. A disabled agency is not crawled at all, so
    nothing else that follows can be said about it; never-succeeded outranks a
    failure count because zero-of-zero is not a streak; and an outright failure
    outranks staleness, which is only the symptom of one.

    Pure: takes values, returns words, touches nothing. The rules are the part
    worth testing, and they are testable without a store or a clock.
    """
    if not enabled:
        return "disabled", "not being crawled"
    if last_success_at is None:
        return "never", ("never crawled successfully" if consec_failures == 0
                         else f"never crawled successfully, {consec_failures} "
                              f"attempt(s) failed")
    if consec_failures >= FAILING_AFTER:
        return "failing", f"{consec_failures} crawls in a row failed"
    if needs_browser:
        return "blocked", "the page needs a browser to render its articles"

    success_age = (now - last_success_at).days
    if success_age >= STALE_AFTER_DAYS:
        return "stale", f"last crawled successfully {success_age} days ago"

    # Its own word rather than a flavour of `stale`, because it is a different
    # fault with a different fix. Stale means we stopped reaching the site;
    # this means we reach it, discovery answers, and nothing survives to
    # storage - everything vetoed as too old or off-pattern, or extraction
    # returning nothing usable. That is the silent failure the whole project
    # exists to make visible, and 92 of the first 324 agencies crawled were in
    # it, so folding it into `stale` would hide the largest bucket on the grid.
    if articles == 0:
        return "empty", "crawled successfully, but no article was ever stored"

    if last_article_at is not None:
        article_age = (now - last_article_at).days
        if article_age >= QUIET_AFTER_DAYS:
            return "quiet", f"crawling fine; nothing published for {article_age} days"
    return "healthy", f"crawled successfully {success_age} days ago"


def compose(frontier: "Frontier", sink: "Sink", *,
            now: datetime | None = None) -> list[AgencyStatus]:
    """Build one row per agency from the local stores.

    The two stores are queried separately and merged here rather than joined in
    SQL, because they are not always the same database: the frontier has a MySQL
    backend for production while the dedup index is always SQLite. A join would
    work today and break on the backend switch, which is the kind of failure
    that shows up as an empty dashboard rather than an error.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    counts = sink.article_stats(since=now - timedelta(days=RECENT_DAYS))

    # One agency can own several newsroom URLs (2 of 2399 do), so the frontier's
    # per-target rows are folded together. Failures are MAXed, not summed: the
    # counter is per target, and adding two independent streaks would report a
    # number that never happened.
    merged: dict[int, AgencyStatus] = {}
    for row in frontier.status_rows():
        (a_id, domain, newsroom_url, enabled, method, last_success,
         failures, p50, needs_browser) = row
        a_id = int(a_id)
        last_success = _as_dt(last_success)

        current = merged.get(a_id)
        if current is None:
            merged[a_id] = AgencyStatus(
                a_id=a_id, domain=domain, newsroom_url=newsroom_url,
                enabled=bool(enabled), discovery_method=method,
                consec_failures=int(failures or 0),
                needs_browser=bool(needs_browser),
                median_body_len=int(p50) if p50 is not None else None,
                last_success_at=last_success,
            )
            continue

        current.targets += 1
        current.enabled = current.enabled or bool(enabled)
        current.needs_browser = current.needs_browser or bool(needs_browser)
        current.consec_failures = max(current.consec_failures, int(failures or 0))
        # The most recently successful target is the one whose method is worth
        # showing - a stale second newsroom should not overwrite the live one.
        if last_success and (current.last_success_at is None
                             or last_success > current.last_success_at):
            current.last_success_at = last_success
            current.discovery_method = method or current.discovery_method
            current.newsroom_url = newsroom_url

    for status in merged.values():
        total, recent, last_article, last_stored = counts.get(
            status.a_id, (0, 0, None, None))
        status.articles = total
        status.articles_recent = recent
        status.last_article_at = _as_dt(last_article)

        # A stored article IS a successful crawl, and it is the only evidence
        # left when the frontier's timestamp is missing. That happens: articles
        # are written as they are extracted but `release_target` runs at the end
        # of the target, so an interrupted pass leaves rows stored and the
        # target unmarked - aacom.org sat that way after one such run. Without
        # this the grid reports "never crawled successfully" about an agency
        # whose articles are sitting in the index, which is a contradiction a
        # person has no way to resolve.
        stored_at = _as_dt(last_stored)
        if stored_at and (status.last_success_at is None
                          or stored_at > status.last_success_at):
            status.last_success_at = stored_at
        status.health, status.reason = classify(
            enabled=status.enabled,
            consec_failures=status.consec_failures,
            needs_browser=status.needs_browser,
            last_success_at=status.last_success_at,
            last_article_at=status.last_article_at,
            articles=status.articles,
            now=now,
        )
        status.severity = severity_of(status.health)
        status.updated_at = now

    return sorted(merged.values(), key=lambda s: s.a_id)


def severity_of(health: str) -> str:
    """The colour band for a health word. Unknown words warn rather than pass."""
    return _SEVERITY.get(health, "warn")


def summarise(rows: Iterable[AgencyStatus]) -> dict[str, int]:
    """Counts per health word, commonest first, for a header line."""
    out: dict[str, int] = {}
    for row in rows:
        out[row.health] = out.get(row.health, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def summary(rows: list[AgencyStatus]) -> dict[str, Any]:
    """The header block the website renders above the grid.

    Deliberately the same shape as `scrapev3_summary()` in `clients/status.php`,
    so a page written against the JSON fixture keeps working when it is pointed
    at the database. Two payloads that differ only in their header is exactly
    the kind of mismatch that shows up as a blank line in production.
    """
    by_health = summarise(rows)
    by_severity = {band: 0 for band in ("error", "warn", "ok")}
    for row in rows:
        by_severity[row.severity] = by_severity.get(row.severity, 0) + 1
    updated = max((r.updated_at for r in rows if r.updated_at), default=None)
    return {
        "total": len(rows),
        "health": by_health,
        "severity": by_severity,
        "updated_at": updated.isoformat(sep=" ", timespec="seconds") if updated
                      else None,
    }


def _as_dt(value: Any) -> datetime | None:
    """Accept what either backend returns: a DATETIME, or SQLite's TEXT."""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip().replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Publishing it
# ---------------------------------------------------------------------------

def connect(settings: Settings) -> Any:
    """Open the state database, creating the schema on first use."""
    from .removal import connect as _connect

    return _connect(settings)


def ensure_table(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def publish(conn: Any, rows: list[AgencyStatus]) -> int:
    """Upsert the whole grid.

    One statement per batch, replacing every column: the row is a snapshot, not
    a log, so there is nothing to merge and no ordering to get wrong. Re-running
    it produces the same table, which is what lets it be safe to run after every
    pass and by hand at the same time.
    """
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    updates = ", ".join(f"{c} = VALUES({c})" for c in COLUMNS if c != "a_id")
    sql = (f"INSERT INTO agency_status ({', '.join(COLUMNS)}) "
           f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}")
    with conn.cursor() as cur:
        cur.executemany(sql, [r.as_row() for r in rows])
    conn.commit()
    log.debug("published status for %d agencies", len(rows))
    return len(rows)


def prune(conn: Any, keep: Iterable[int]) -> int:
    """Delete rows for agencies the crawler no longer holds.

    Without this a removed agency would keep its last-known status on the
    website's grid forever - still green, still listed, and no longer crawled.
    A removal has to reach the dashboard too or it is not a removal.
    """
    keep = sorted(set(int(a) for a in keep))
    with conn.cursor() as cur:
        if not keep:
            cur.execute("DELETE FROM agency_status")
        else:
            marks = ", ".join(["%s"] * len(keep))
            cur.execute(f"DELETE FROM agency_status WHERE a_id NOT IN ({marks})",
                        tuple(keep))
        deleted = cur.rowcount
    conn.commit()
    if deleted:
        log.debug("pruned %d status row(s) for agencies no longer held", deleted)
    return deleted


def to_json(rows: list[AgencyStatus], *, indent: int = 2) -> str:
    """The same payload the website reads, as a file.

    Written for `clients/status_demo.php`, which renders from this when no
    database is configured - so the page can be opened and judged before any of
    the wiring exists.
    """
    payload = {
        "generated_at": datetime.now(timezone.utc).replace(
            tzinfo=None).isoformat(sep=" ", timespec="seconds"),
        "summary": summary(rows),
        "agencies": [r.as_dict() for r in rows],
    }
    return json.dumps(payload, indent=indent)
