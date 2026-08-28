"""The polite fetcher.

Design notes worth keeping in mind when editing this file:

* Pacing is keyed on the REGISTRABLE DOMAIN, with a secondary cap per IP.

  Domain is the primary key because news.example.com and www.example.com are
  usually one server and one blast radius. Measured on the real corpus: all 24
  sampled house.gov hostnames resolve to a single Akamai IP, so its 417
  legislator press pages genuinely are one origin.

  The IP cap exists because the Phase 1 survey found the inverse case too -
  28.3% of domains share an IP with another domain, largest cluster 28. Those
  clusters are managed-hosting edges (WP Engine 141.193.213.x, Pantheon
  23.185.0.x) fronting hundreds of unrelated customers. Serialising them like a
  single origin would be absurdly over-conservative, but an unbounded 28-way
  concurrent burst at one edge is worth capping. So: strict pacing per domain,
  a loose concurrency ceiling per IP.

* Concurrency per host is 1, enforced by a lock, not by convention. Combined
  with the domain-lease frontier this makes it structurally impossible to have
  two in-flight requests to the same publisher.

* Jitter is mandatory. At daily cadence the aggregate rate is ~1.5 req/s across
  50k sites - trivially polite on average. The only real risk is BURSTINESS,
  so we never emit evenly-spaced or contiguous requests to one host.

* Latency-adaptive backoff: if a host slows down we back off even on HTTP 200.
  We are the marginal load on someone else's server; back off before the
  operator notices, not after they block us.
"""

from __future__ import annotations

import asyncio
import random
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession

from ..settings import Settings
from ..urls import registrable_domain
from .robots import RobotsRules, parse_robots

# A bot wall returns HTTP 200 with plausible HTML, so status codes alone miss
# it. The page <title> is the cheapest reliable tell. v2's logs recorded 47 of
# these across ~2,405 sites (~2%).
WALL_MARKERS = (
    "just a moment",
    "access denied",
    "attention required",
    "are you human",
    "verify you are human",
    "checking your browser",
    "security check",
    "ddos protection",
    "one more step",
)


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    text: str
    headers: dict[str, str]
    elapsed_s: float
    from_cache: bool = False          # 304 Not Modified
    wall: str | None = None           # bot-wall marker found in <title>
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.wall is None and 200 <= self.status < 300

    @property
    def html(self) -> str:
        return self.text


@dataclass
class _HostState:
    """Per-registrable-domain pacing state."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_allowed_at: float = 0.0      # monotonic
    delay_s: float | None = None      # effective delay, may grow via backoff
    latencies: list[float] = field(default_factory=list)
    consec_failures: int = 0
    blocked_until: float = 0.0        # circuit breaker
    # What opened the breaker. Without it the breaker hides the very thing it
    # is reacting to: a run reported "circuit-open x11" and nothing else,
    # because the failures that tripped it happened during discovery and the
    # article fetches that followed only ever saw the open circuit.
    blocked_reason: str | None = None

    def observe_latency(self, seconds: float) -> None:
        self.latencies.append(seconds)
        if len(self.latencies) > 20:
            del self.latencies[0]

    def p50(self) -> float | None:
        if len(self.latencies) < 5:
            return None
        ordered = sorted(self.latencies)
        return ordered[len(ordered) // 2]


# Failures where no server ever answered, so there is nothing to conclude about
# the URL itself - only about the host name. Formatted by _raw_get as
# "{type(exc).__name__}: {exc}".
_NO_SERVER_ERRORS = ("DNSError", "SSLError", "ConnectionError", "ConnectTimeout")


def _with_www(url: str) -> str | None:
    """The same URL on the `www.` host, or None if there is no such variant.

    Deliberately not restricted to apex hosts. `law.georgetown.edu` does not
    resolve while `www.law.georgetown.edu` does, so a subdomain needs this
    exactly as much as `ardian.com` does - the caller's gate is "no server ever
    answered", and against a host that is already answering this is never
    reached. Shape of the hostname is not evidence about what resolves.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or host.startswith("www."):
        return None
    return urlunsplit((parts.scheme, f"www.{parts.netloc}", parts.path,
                       parts.query, parts.fragment))


# Challenge pages that return HTTP 200 with a script where the content should
# be. The <title> tell does not fire on these: justice.gov serves 2.6 KB
# containing triggerInterstitialChallenge() and no text at all, which read as
# "needs_browser: low text yield" - so every article was queued for a browser
# that would meet the same challenge, and the host was never backed off from.
# These are vendor fingerprints, not per-site knowledge.
CHALLENGE_MARKERS = (
    "triggerinterstitialchallenge",                   # Akamai Bot Manager
    "_cf_chl_opt", "__cf_chl", "challenge-platform",  # Cloudflare
    "_incapsula_resource",                            # Imperva
    "awswaf",                                         # AWS WAF
    "_pxhd", "px-captcha",                            # PerimeterX
    "kpsdk",                                          # Kasada
)
# Above this the page carries real content, and any such string in it is
# incidental - an article about bot protection is still an article.
CHALLENGE_MAX_BYTES = 8192


def detect_wall(html: str) -> str | None:
    """Return the matched marker if the page looks like a challenge page.

    Two tells, because a wall arrives two ways: as a rendered notice whose
    <title> says so, and as a bare script that would run in a browser. Both
    return HTTP 200 with plausible bytes, which is why status codes miss them.
    """
    if not html:
        return None

    if len(html) <= CHALLENGE_MAX_BYTES:
        small = html.lower()
        for marker in CHALLENGE_MARKERS:
            if marker in small:
                return f"js challenge ({marker})"

    lowered = html[:4096].lower()
    start = lowered.find("<title")
    if start == -1:
        return None
    open_end = lowered.find(">", start)
    end = lowered.find("</title>", open_end)
    if open_end == -1 or end == -1:
        return None
    title = lowered[open_end + 1:end].strip()
    for marker in WALL_MARKERS:
        if marker in title:
            return title[:120]
    return None


class PoliteFetcher:
    """Async HTTP client that cannot be impolite by construction."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self._hosts: dict[str, _HostState] = {}
        self._robots: dict[str, RobotsRules] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._session: AsyncSession | None = None
        self._global_sem = asyncio.Semaphore(self.settings.politeness.global_concurrency)
        # Secondary per-IP constraint - see settings.max_concurrency_per_ip.
        self._dns_cache: dict[str, str | None] = {}
        self._ip_sems: dict[str, asyncio.Semaphore] = {}

    async def _resolve(self, host: str) -> str | None:
        """Resolve and cache a hostname, so pacing can key on IP as well.

        Pre-resolving also keeps DNS off the request critical path. Python's
        default resolver runs getaddrinfo on a bounded thread pool, so a
        handful of dead NS records with multi-second SERVFAIL timeouts will
        otherwise stall every other lookup in the pool.
        """
        if host in self._dns_cache:
            return self._dns_cache[host]
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP),
                timeout=self.settings.politeness.dns_timeout_s,
            )
            ip = infos[0][4][0] if infos else None
        except Exception:
            ip = None
        self._dns_cache[host] = ip
        return ip

    def _ip_sem(self, ip: str) -> asyncio.Semaphore:
        sem = self._ip_sems.get(ip)
        if sem is None:
            sem = asyncio.Semaphore(self.settings.politeness.max_concurrency_per_ip)
            self._ip_sems[ip] = sem
        return sem

    async def __aenter__(self) -> "PoliteFetcher":
        self._session = AsyncSession(
            timeout=self.settings.politeness.request_timeout_s,
            impersonate=self.settings.identity.impersonate,
            # HTTP/1.1 with keepalive. Under strict per-host politeness there is
            # exactly one request in flight per host, so HTTP/2 multiplexing has
            # nothing to multiplex; the value is amortizing one TLS handshake
            # across a lease's burst of article fetches.
            allow_redirects=True,
            # Redirects are followed inside curl, so they bypass per-host
            # pacing entirely. Capped well below curl's default of 30 to bound
            # what one misconfigured site can extract from us in a single get().
            max_redirects=self.settings.politeness.max_redirects,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- pacing ------------------------------------------------------------

    def _host_state(self, domain: str) -> _HostState:
        state = self._hosts.get(domain)
        if state is None:
            state = _HostState()
            self._hosts[domain] = state
        return state

    def _effective_delay(self, domain: str, robots: RobotsRules | None) -> float:
        state = self._host_state(domain)
        if state.delay_s is not None:
            return state.delay_s
        base = self.settings.politeness.default_delay_s
        if robots is not None:
            declared = robots.crawl_delay(self.settings.identity.user_agent)
            if declared is not None:
                base = max(base, declared)   # Crawl-delay is a FLOOR, never a ceiling
        state.delay_s = base
        return base

    async def _wait_turn(self, domain: str, delay: float) -> None:
        now = time.monotonic()
        state = self._host_state(domain)
        if now < state.next_allowed_at:
            await asyncio.sleep(state.next_allowed_at - now)
        jitter = 1.0 + random.uniform(-self.settings.politeness.jitter_pct,
                                      self.settings.politeness.jitter_pct)
        state.next_allowed_at = time.monotonic() + max(0.0, delay * jitter)

    # -- robots ------------------------------------------------------------

    async def robots_for(self, url: str) -> RobotsRules:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is not None:
            return cached

        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._robots.get(origin)
            if cached is not None:
                return cached
            resp = await self._raw_get(f"{origin}/robots.txt", paced=True)
            rules = parse_robots(origin, resp.text if resp.error is None else "", resp.status)
            self._robots[origin] = rules
            return rules

    # -- fetching ----------------------------------------------------------

    async def _raw_get(
        self,
        url: str,
        *,
        paced: bool,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Response:
        assert self._session is not None, "use `async with PoliteFetcher() as f:`"
        domain = registrable_domain(url)
        state = self._host_state(domain)

        headers = dict(self.settings.identity.headers())
        # Conditional GET. Google recommends ETag over Last-Modified: a file
        # re-saved with identical content gets a new timestamp and triggers a
        # pointless refetch. Applied mostly to feeds/listings, which we re-poll
        # constantly and which rarely change.
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        async with self._global_sem:
            async with state.lock:          # concurrency-per-host == 1
                if paced:
                    await self._wait_turn(domain, self._effective_delay(
                        domain, self._robots.get(origin)))

                # Resolve outside the IP semaphore, and hold that semaphore
                # only for the request itself - never across the pacing wait,
                # which would serialise every domain on a shared CDN edge.
                ip = await self._resolve(parts.hostname or "")
                ip_sem = self._ip_sem(ip) if ip else None

                started = time.monotonic()
                try:
                    if ip_sem is not None:
                        async with ip_sem:
                            r = await self._session.get(url, headers=headers)
                    else:
                        r = await self._session.get(url, headers=headers)
                except Exception as exc:                      # noqa: BLE001
                    state.consec_failures += 1
                    # Same verdict as a run of 403s, reached a different way: a
                    # host failing every request at the transport layer is not
                    # going to serve the next one either. ersnet.org 301s a
                    # section to itself forever, so ten article fetches each
                    # burned the full redirect budget before failing.
                    pol = self.settings.politeness
                    if state.consec_failures >= pol.max_consec_refusals:
                        state.blocked_until = time.monotonic() + pol.refusal_cooldown_s
                        state.blocked_reason = (
                            f"{state.consec_failures}x {type(exc).__name__}")
                    return Response(url=url, final_url=url, status=0, text="",
                                    headers={}, elapsed_s=time.monotonic() - started,
                                    error=f"{type(exc).__name__}: {exc}")
                elapsed = time.monotonic() - started

        state.observe_latency(elapsed)
        text = ""
        if r.status_code != 304:
            try:
                text = r.text
            except Exception:
                text = ""

        resp = Response(
            url=url,
            final_url=str(r.url),
            status=r.status_code,
            text=text,
            headers={k.lower(): v for k, v in dict(r.headers).items()},
            elapsed_s=elapsed,
            from_cache=(r.status_code == 304),
            wall=detect_wall(text),
        )

        self._apply_backoff(state, resp)
        return resp

    def _apply_backoff(self, state: _HostState, resp: Response) -> None:
        """Adjust this host's delay based on what just happened."""
        pol = self.settings.politeness

        if resp.status in (429, 503):
            retry_after = resp.headers.get("retry-after")
            wait = 60.0
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 60.0        # HTTP-date form; 60s is a safe floor
            state.blocked_until = time.monotonic() + min(wait, 3600.0)
            state.blocked_reason = f"HTTP {resp.status}"
            state.delay_s = min((state.delay_s or pol.default_delay_s) * 2, 300.0)
            state.consec_failures += 1
            return

        if resp.status == 403 or resp.wall:
            state.consec_failures += 1
            # A run of refusals is a verdict, not a transient fault. Without
            # this the crawler kept asking: news.csub.edu refused fourteen
            # consecutive article fetches in one pass, each after the full
            # per-host delay, and would have done so again on every daily run.
            if state.consec_failures >= pol.max_consec_refusals:
                state.blocked_until = time.monotonic() + pol.refusal_cooldown_s
                state.blocked_reason = (
                    f"{state.consec_failures}x "
                    f"{resp.wall and 'bot wall' or f'HTTP {resp.status}'}")
            return

        if resp.ok or resp.from_cache:
            state.consec_failures = 0
            state.blocked_reason = None
            # Latency-adaptive backoff, in both directions.
            p50 = state.p50()
            if p50 and resp.elapsed_s > p50 * pol.latency_backoff_multiple:
                state.delay_s = min((state.delay_s or pol.default_delay_s) * 2, 120.0)
            elif state.delay_s and state.delay_s > pol.default_delay_s:
                # Additive-increase / multiplicative-decrease: recover slowly.
                state.delay_s = max(pol.default_delay_s, state.delay_s - 1.0)

    async def get(
        self,
        url: str,
        *,
        check_robots: bool = True,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Response:
        """Fetch a URL politely, honoring robots.txt and per-host pacing.

        Falls back to the `www.` host when the bare one never reached a server.
        Canonicalisation strips `www.`, which is right for identity - one
        article should not be two records because it was linked both ways - but
        it is not an assertion that the apex host serves anything. Two of the
        ten domains in one run were unreachable for exactly that reason:
        `ardian.com` has no A record at all, and `escardio.org` resolves to a
        different host than `www.escardio.org` and fails TLS there.

        Retried only on DNS/TLS/connection failure - i.e. when no server ever
        answered - so a genuine 404 or 403 is never re-requested, and a working
        host never pays for this. The retry runs the robots check again for the
        new origin and takes the same per-host lock and delay, so it is exactly
        as polite as the request it replaces.
        """
        resp = await self._get_once(url, check_robots=check_robots,
                                    etag=etag, last_modified=last_modified)
        if resp.error and resp.error.startswith(_NO_SERVER_ERRORS):
            alternative = _with_www(url)
            if alternative is not None:
                retried = await self._get_once(alternative, check_robots=check_robots,
                                               etag=etag, last_modified=last_modified)
                if retried.ok:
                    return retried
        return resp

    async def _get_once(
        self,
        url: str,
        *,
        check_robots: bool = True,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Response:
        """One attempt: circuit breaker, robots, pacing, fetch."""
        domain = registrable_domain(url)
        state = self._host_state(domain)

        now = time.monotonic()
        if now < state.blocked_until:
            because = f" after {state.blocked_reason}" if state.blocked_reason else ""
            return Response(url=url, final_url=url, status=0, text="", headers={},
                            elapsed_s=0.0,
                            error=f"circuit-open: host backing off{because}")

        if check_robots:
            rules = await self.robots_for(url)
            if not rules.allows(url, self.settings.identity.user_agent):
                return Response(url=url, final_url=url, status=0, text="", headers={},
                                elapsed_s=0.0, error="robots-disallow")

        return await self._raw_get(url, paced=True, etag=etag, last_modified=last_modified)
