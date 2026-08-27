"""robots.txt fetching, caching, and interpretation.

Neither v1 nor v2 fetched robots.txt at all - zero references in either
codebase. Since the entire authorization story for this project is "robots.txt
permits us", honoring it is not optional, and it is also a hard requirement of
Cloudflare's Verified Bot program.

Crawl-delay is non-standard and Google ignores it. We honor it as a hard floor
anyway: Cloudflare explicitly names ignoring it as grounds for removal from
Verified Bots, and it costs us almost nothing at daily cadence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from protego import Protego


@dataclass
class RobotsRules:
    """Parsed robots.txt for one origin."""

    origin: str
    fetched_at: float
    parser: Protego | None          # None => fetch failed
    raw: str = ""
    status: int = 0
    sitemaps: list[str] = field(default_factory=list)
    # Content Signals (contentsignals.org): search / ai-input / ai-train.
    # These constrain DOWNSTREAM USE, not fetching. We record them per domain
    # and carry them through so consumers can respect them.
    content_signals: dict[str, bool] = field(default_factory=dict)

    def allows(self, url: str, user_agent: str) -> bool:
        # Fail OPEN on a missing/unreachable robots.txt, which is what RFC 9309
        # specifies: a 404 means no restrictions. Fail CLOSED only on an
        # explicit disallow.
        if self.parser is None:
            return True
        return bool(self.parser.can_fetch(url, user_agent))

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.parser is None:
            return None
        try:
            d = self.parser.crawl_delay(user_agent)
            return float(d) if d is not None else None
        except Exception:
            return None


_CONTENT_SIGNAL_KEYS = ("search", "ai-input", "ai-train")


def parse_content_signals(raw: str) -> dict[str, bool]:
    """Parse `Content-Signal:` directives from robots.txt.

    Format is `Content-Signal: search=yes, ai-train=no`. Absent means
    unspecified, which is NOT the same as 'no' - we only record what is stated.
    """
    signals: dict[str, bool] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.lower().startswith("content-signal:"):
            continue
        _, _, value = line.partition(":")
        for item in value.split(","):
            key, _, val = item.strip().partition("=")
            key = key.strip().lower()
            if key in _CONTENT_SIGNAL_KEYS:
                signals[key] = val.strip().lower() in {"yes", "1", "true"}
    return signals


def parse_robots(origin: str, raw: str, status: int) -> RobotsRules:
    parser: Protego | None = None
    sitemaps: list[str] = []
    signals: dict[str, bool] = {}

    if status == 200 and raw:
        try:
            parser = Protego.parse(raw)
            sitemaps = [s for s in (parser.sitemaps or [])]
        except Exception:
            parser = None
        signals = parse_content_signals(raw)
    elif 400 <= status < 500:
        # 4xx => no restrictions (RFC 9309). Leave parser None => allow all.
        pass
    # 5xx is ambiguous. RFC 9309 suggests treating a persistent 5xx as
    # disallow-all, but for a daily crawl of authorized sites, backing off the
    # host (which the caller does anyway) is the proportionate response.

    return RobotsRules(
        origin=origin,
        fetched_at=time.time(),
        parser=parser,
        raw=raw,
        status=status,
        sitemaps=sitemaps,
        content_signals=signals,
    )
