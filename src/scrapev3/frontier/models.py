"""Frontier data types.

Timestamps are stored as 'YYYY-MM-DD HH:MM:SS' UTC strings in both backends.
That format sorts correctly under SQLite's lexical TEXT comparison and is a
native DATETIME in MySQL, so one representation works for both without the DAL
having to translate comparison operators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
EPOCH = "1970-01-01 00:00:00"
# MySQL's maximum DATETIME, and lexically last under SQLite's TEXT comparison -
# so comparing against it means "no upper bound" in either backend without the
# query having to change shape.
FOREVER = "9999-12-31 23:59:59"

# Backoff is applied to the domain itself, so a dead site stops costing us
# anything. v2 logged 21 dnsNotFound errors with no such mechanism - it retried
# dead domains on every run, forever.
MAX_BACKOFF_EXPONENT = 6

# How long a 'this site has no feed' verdict is trusted before re-probing.
FEED_ABSENCE_TTL_DAYS = 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


def from_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], TS_FORMAT)
    except ValueError:
        return None


@dataclass
class Target:
    """One newsroom URL. Many targets may share one domain.

    This distinction is load-bearing: house.gov alone has 417 legislator press
    pages. They must be *paced* as one origin (they resolve to a single Akamai
    IP) but all 417 still have to be crawled. Leasing is per domain; crawling
    is per target.
    """

    newsroom_url: str
    domain: str
    a_id: int
    enabled: bool = True
    discovery_method: str | None = None
    feed_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_success_at: datetime | None = None
    consec_failures: int = 0
    p50_body_len: int | None = None
    # Negative cache: this target was probed for a feed and had none. Saves
    # ~45s per run (nine paths at the per-host delay) on feedless sites.
    feed_absent: bool = False
    probed_at: datetime | None = None

    def feed_absence_is_fresh(self, ttl_days: int = FEED_ABSENCE_TTL_DAYS) -> bool:
        """Trust the cached 'no feed' verdict only while it is recent.

        Sites do add feeds. Without an expiry a single probe would blacklist a
        domain's feed forever, and we would never notice it appearing.
        """
        if not self.feed_absent or self.probed_at is None:
            return False
        return (utcnow() - self.probed_at) < timedelta(days=ttl_days)


@dataclass
class DomainRecord:
    """One row of `domain_state` - a crawl target and its schedule."""

    domain: str
    a_id: int
    newsroom_url: str
    shard: int
    enabled: bool = True
    next_allowed_at: datetime = field(default_factory=utcnow)
    leased_until: datetime | None = None
    lease_owner: str | None = None
    crawl_delay_s: float = 5.0
    revisit_period_s: int = 86_400          # daily cadence
    consec_failures: int = 0
    discovery_method: str | None = None
    feed_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    needs_browser: bool = False
    needs_browser_at: datetime | None = None
    last_success_at: datetime | None = None
    p50_body_len: int | None = None
    # Populated by Frontier.acquire(); every newsroom URL on this domain.
    targets: list[Target] = field(default_factory=list)

    def backoff_seconds(self) -> int:
        """Delay until the next attempt, doubling per consecutive failure."""
        exponent = min(self.consec_failures, MAX_BACKOFF_EXPONENT)
        return self.revisit_period_s * (2 ** exponent)


@dataclass
class FrontierStats:
    total: int = 0
    targets: int = 0
    enabled: int = 0
    due: int = 0
    leased: int = 0
    failing: int = 0
    never_crawled: int = 0
    needs_browser: int = 0
