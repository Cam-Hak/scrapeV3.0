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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession

from ..settings import Settings
from ..tracing import get as _get_logger, tag
from ..urls import registrable_domain
from .robots import RobotsRules, parse_robots

log = _get_logger(__name__)

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
    def reached(self) -> bool:
        """The origin answered. A 304 is a successful fetch carrying no body.

        `ok` cannot express this, because `ok` also means "there is content
        here to parse" and every caller relies on that. Arming conditional GET
        without a separate word for it would make every unchanged feed read as
        a failed fetch: discovery returns `method="none"`, the target is
        released with `success=False`, the persistent failure counter climbs,
        and three days later the website tells the publisher their site is
        failing - because nothing had changed on it. That is a worse bug than
        the request volume it was meant to save.
        """
        return self.error is None and self.wall is None and (self.ok or self.from_cache)

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
    # The last refusal this host served, kept so the frontier can record WHAT
    # refused us and not merely that something did. A challenge and a flat
    # denial need different words: one might be worth a browser, the other is
    # the publisher saying no.
    last_wall: str | None = None
    last_refused_status: int = 0

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


def failure_kind(resp: "Response") -> str:
    """One word for why this fetch produced nothing. Pure; no I/O.

    `status = 0` is produced at four unrelated places - a transport exception,
    an open circuit breaker, a robots refusal, and a Response nobody filled in
    - and every consumer downstream saw the same undifferentiated zero. The
    audit stored it and threw the reason away, so 67 targets read as "the
    publisher's site is down" when 20 of them were our own resolver failing to
    answer for `.mil` at all. `nslookup www.centcom.mil 1.1.1.1` returns an
    address instantly; the local resolver returned `getaddrinfo failed`.

    Blaming a publisher for our own broken resolver is the same class of
    silent-quality bug as crediting a body to the wrong source: nothing raises,
    and the wrong answer is perfectly plausible.

    A CLOSED vocabulary, for the reason `severity` is closed in `status.py`:
    this ends up on a website nobody has redeployed, so a word invented later
    must not silently mean "fine".
    """
    if resp.wall:
        return "wall"
    if resp.error:
        err = resp.error
        if err.startswith("robots-disallow"):
            return "robots"          # not a failure at all - the rule working
        if err.startswith("circuit-open"):
            return "circuit"
        if err.startswith("DNSError"):
            return "dns"
        # curl_cffi raises CertificateVerifyError rather than SSLError for an
        # expired or untrusted chain, and the re-audit found 8 targets landing
        # in the catch-all "error" bucket for exactly that reason - a distinct,
        # actionable fault reported as "something went wrong".
        if err.startswith(("SSLError", "CertificateVerifyError")):
            return "tls"
        # "HTTP/2 stream N reset by server (INTERNAL_ERROR)" - 7 targets in the
        # re-audit. Its own word because it is the one failure here that is
        # about the PROTOCOL rather than the site: the impersonation profile
        # negotiates h2 and these servers cannot hold the stream open. Retrying
        # such a host on HTTP/1.1 is the obvious next move, and it needs a name
        # before it can be counted.
        if err.startswith("HTTPError") and "HTTP/2" in err:
            return "http2"
        if err.startswith(("ConnectionError", "ConnectTimeout")):
            return "connect"
        if err.startswith(("Timeout", "browser-timeout")):
            return "timeout"
        return "error"
    if resp.from_cache:
        return "not_modified"
    if resp.ok:
        return "ok"
    if 400 <= resp.status < 500:
        return "http_4xx"
    if resp.status >= 500:
        return "http_5xx"
    return "error"


# Which refusals a browser could plausibly get past, and which it could not.
# The distinction is load-bearing rather than cosmetic: of 41 walls in the
# first corpus run, 30 were "access denied" - a flat refusal that renders the
# same in Chrome - and only ~11 were interstitials that solve themselves once
# JavaScript runs. Writing `needs_browser` for all 41 would put "the page needs
# a browser to render its articles" on 30 publishers' rows as a false
# statement, and queue browser work certain to fail.
_CHALLENGE_WALLS = (
    "just a moment",
    "checking your browser",
    "are you human",
    "verify you are human",
    "security check",
    "one more step",
    "js challenge",              # the CHALLENGE_MARKERS prefix
)


@dataclass(frozen=True)
class HostVerdict:
    """What a host did to us, in a word the frontier can store.

    `access` is a CLOSED vocabulary, like `severity`:

      challenge   an interstitial that JavaScript would clear - maybe a browser
      refused     a flat denial. This is a site declining an identified
                  crawler, and no amount of rendering changes it
      unresolved  our own resolver could not answer. Ours to fix, not theirs
    """

    access: str
    reason: str
    failures: int


def _retry_after_seconds(raw: str | None, default: float = 60.0) -> float:
    """`Retry-After`, in seconds, from either form RFC 9110 allows.

    The delta-seconds form is a bare integer; the other is an HTTP date. Only
    the first was parsed, and the date form fell back to a flat 60 seconds -
    so a server saying "come back in four hours" was asked again in one
    minute, which is precisely the message it was trying not to have to send
    again. Both forms are now believed.

    Anything unparseable still floors at `default` rather than 0: a malformed
    header is not permission to retry immediately.
    """
    if not raw:
        return default
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when is None:
            return default
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:                                       # noqa: BLE001
        return default


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

    def __init__(self, settings: Settings | None = None,
                 escalate: dict[str, str] | None = None):
        self.settings = settings or Settings.load()
        # domain -> access verdict, supplied by the frontier. The fetcher never
        # reads the frontier itself, and nothing here names a site: this is
        # per-domain DATA, which is where CLAUDE.md puts site-specific facts.
        self._escalate: dict[str, str] = dict(escalate or {})
        self._browser = None
        self._hosts: dict[str, _HostState] = {}
        self._robots: dict[str, RobotsRules] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._session: AsyncSession | None = None
        self._global_sem = asyncio.Semaphore(self.settings.politeness.global_concurrency)
        # Secondary per-IP constraint - see settings.max_concurrency_per_ip.
        self._dns_cache: dict[str, str | None] = {}
        self._ip_sems: dict[str, asyncio.Semaphore] = {}
        # Hosts the resolver could not answer for, and why. `_resolve` already
        # learned this and threw it away, so a resolver broken for a whole TLD
        # looked exactly like 20 publishers whose sites were down.
        self._dns_failures: dict[str, str] = {}
        # Which User-Agent this host accepted, learned once per run after a
        # refusal. Data keyed on a domain, never a branch naming one.
        self._identity_for: dict[str, str] = {}

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
        except Exception as exc:                            # noqa: BLE001
            ip = None
            # Remembered, not swallowed. The distinction between "this host
            # does not exist" and "our resolver cannot answer for it" is the
            # difference between a finding against the publisher and a finding
            # against us, and only this line knows which one happened.
            self._dns_failures[host] = type(exc).__name__

        # curl resolves the request itself, so a DoH-configured run fetches
        # these hosts fine - but the per-IP concurrency cap keys on what WE
        # resolved, and an unresolved host silently opts out of it. Ask the
        # same DoH resolver curl is using, so the cap keeps applying to
        # exactly the hosts that most need it: .mil is one Akamai edge.
        if ip is None and self.settings.politeness.doh_url:
            ip = await self._resolve_doh(host)
            if ip is not None:
                self._dns_failures.pop(host, None)

        self._dns_cache[host] = ip
        return ip

    async def _resolve_doh(self, host: str) -> str | None:
        """Resolve one A record through the configured DoH endpoint.

        The JSON form (RFC 8484's `application/dns-json` companion), because
        building and parsing wire-format DNS to learn one address would be a
        parser we then have to own.
        """
        if self._session is None:
            return None
        try:
            r = await asyncio.wait_for(
                self._session.get(
                    self.settings.politeness.doh_url,
                    params={"name": host, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                ),
                timeout=self.settings.politeness.dns_timeout_s * 2,
            )
            for answer in (r.json().get("Answer") or []):
                if answer.get("type") == 1:          # A record
                    return answer.get("data")
        except Exception:                                   # noqa: BLE001
            return None
        return None

    def host_verdict(self, domain: str) -> "HostVerdict | None":
        """What this host did to us, for the frontier to remember. Read-only.

        The in-process refusal counter dies with the fetcher, and the
        frontier's own counter never learned *why* - so `needs_browser` sat
        wired end-to-end, through the status table and onto the website, with
        nothing on earth writing it. This is the missing half.
        """
        state = self._hosts.get(domain)
        host = ""
        for name in self._dns_failures:
            if name == domain or name.endswith("." + domain):
                host = name
                break
        if host:
            return HostVerdict("unresolved",
                               "our resolver could not resolve this host",
                               state.consec_failures if state else 0)
        if state is None or not state.consec_failures:
            return None
        if state.last_wall:
            wall = state.last_wall.lower()
            if any(marker in wall for marker in _CHALLENGE_WALLS):
                return HostVerdict("challenge", state.last_wall,
                                   state.consec_failures)
            return HostVerdict("refused", state.last_wall, state.consec_failures)
        if state.last_refused_status == 403:
            return HostVerdict("refused", "HTTP 403", state.consec_failures)
        return None

    @asynccontextmanager
    async def _paced(self, domain: str, hostname: str, origin: str):
        """Hold every pacing control for the duration of one fetch.

        Lifted out of `_raw_get` so a second transport cannot accidentally be
        less polite than the first. With the browser tier reusing this, "the
        browser is exactly as polite as the fetcher" is a property of the code
        rather than a sentence in a comment - it takes the global semaphore,
        the per-host lock, the delay with jitter, and the per-IP cap, in that
        order, because holding the IP semaphore across the pacing wait would
        serialise every domain sharing a CDN edge.

        Yields the per-IP semaphore (or None), which the caller holds only
        around the request itself.
        """
        state = self._host_state(domain)
        async with self._global_sem:
            async with state.lock:          # concurrency-per-host == 1
                await self._wait_turn(domain, self._effective_delay(
                    domain, self._robots.get(origin)))
                ip = await self._resolve(hostname)
                yield self._ip_sem(ip) if ip else None

    def resolver_report(self) -> tuple[int, int]:
        """(hosts attempted, hosts that failed to resolve) for this run.

        Read-only. A run where most hostnames fail to resolve is a local
        infrastructure fault, and saying so once is worth more than saying
        "site unreachable" once per publisher.
        """
        return len(self._dns_cache), len(self._dns_failures)

    def _ip_sem(self, ip: str) -> asyncio.Semaphore:
        sem = self._ip_sems.get(ip)
        if sem is None:
            sem = asyncio.Semaphore(self.settings.politeness.max_concurrency_per_ip)
            self._ip_sems[ip] = sem
        return sem

    async def __aenter__(self) -> "PoliteFetcher":
        # Whatever ALPN the impersonation profile negotiates, kept deliberately
        # unset. An earlier comment here claimed "HTTP/1.1 with keepalive", but
        # no `http_version` was ever passed, so it described a decision the
        # code never made - and a Chrome TLS fingerprint that then refuses the
        # h2 every real Chrome negotiates would be its own cross-layer
        # anomaly. The pacing argument it made is sound and unaffected either
        # way: one request in flight per host has nothing to multiplex.
        extra = {}
        if self.settings.politeness.doh_url:
            extra["doh_url"] = self.settings.politeness.doh_url

        self._session = AsyncSession(
            timeout=self.settings.politeness.request_timeout_s,
            impersonate=self.settings.identity.impersonate,
            allow_redirects=True,
            **extra,
            # Redirects are followed inside curl, so they bypass per-host
            # pacing entirely. Capped well below curl's default of 30 to bound
            # what one misconfigured site can extract from us in a single get().
            max_redirects=self.settings.politeness.max_redirects,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        # Before the session, and unconditionally: an orphaned Chrome on a
        # nightly cron is a real operational failure, not a tidy-up detail.
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
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
            declared = robots.crawl_delay(self.settings.identity.robots_agent)
            if declared is not None:
                base = max(base, declared)   # Crawl-delay is a FLOOR, never a ceiling
        state.delay_s = base
        return base

    async def _wait_turn(self, domain: str, delay: float) -> None:
        now = time.monotonic()
        state = self._host_state(domain)
        if now < state.next_allowed_at:
            waited = state.next_allowed_at - now
            log.debug("%s     wait %.1fs", tag(domain), waited)
            await asyncio.sleep(waited)
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
        user_agent: str | None = None,
    ) -> Response:
        assert self._session is not None, "use `async with PoliteFetcher() as f:`"
        domain = registrable_domain(url)
        state = self._host_state(domain)

        headers = dict(self.settings.identity.headers(user_agent))
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
                    # When our own resolver already failed for this hostname,
                    # say so plainly. curl reports its own DNS failure as
                    # DNSError, but a connect error against a host we never
                    # resolved is the same fault wearing a different name, and
                    # `failure_kind` has to be able to tell.
                    name = type(exc).__name__
                    if (parts.hostname or "") in self._dns_failures:
                        name = "DNSError"
                    return Response(url=url, final_url=url, status=0, text="",
                                    headers={}, elapsed_s=time.monotonic() - started,
                                    error=f"{name}: {exc}")
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
            wait = _retry_after_seconds(resp.headers.get("retry-after"))
            state.blocked_until = time.monotonic() + min(wait, 3600.0)
            state.blocked_reason = f"HTTP {resp.status}"
            state.delay_s = min((state.delay_s or pol.default_delay_s) * 2, 300.0)
            state.consec_failures += 1
            return

        if resp.status == 403 or resp.wall:
            state.consec_failures += 1
            state.last_wall = resp.wall or state.last_wall
            state.last_refused_status = resp.status or state.last_refused_status
            # Slow down as well as count. The counter alone let a refusing host
            # be re-asked at the ordinary 5s cadence four more times before the
            # breaker opened - the same knocking the breaker exists to stop,
            # just under the threshold. Backing off on the first refusal makes
            # the run-up to the breaker quieter, not only shorter.
            state.delay_s = min((state.delay_s or pol.default_delay_s) * 2, 300.0)
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
        domain = registrable_domain(url)
        resp = await self._get_once(url, check_robots=check_robots,
                                    etag=etag, last_modified=last_modified,
                                    user_agent=self._identity_for.get(domain))
        if resp.error and resp.error.startswith(_NO_SERVER_ERRORS):
            alternative = _with_www(url)
            if alternative is not None:
                retried = await self._get_once(
                    alternative, check_robots=check_robots, etag=etag,
                    last_modified=last_modified,
                    user_agent=self._identity_for.get(domain))
                if retried.ok:
                    return retried
            return resp

        # The refusal retry. A 403 or a wall on the FIRST request is the only
        # thing that reaches for the fallback identity, and it is tried once
        # per host per run and then remembered, so a refusing host costs one
        # extra request in a run rather than one per URL.
        #
        # Measured: holding TLS and every other header constant, defense.gov,
        # weforum.org and michigan.gov return 403 to the bot User-Agent and
        # 200 to a browser one, and all three publish a robots.txt that allows
        # us. The CDN is overriding the publisher's own stated policy, so this
        # asks the same question a second time rather than asking a different
        # one - `From:` is still sent, robots is still evaluated against
        # `robots_agent`, and the retry runs through `_get_once`, so it takes
        # the circuit breaker, the robots check, the host lock and the full
        # delay exactly like any other request.
        if (self._should_retry_identity(resp)
                and domain not in self._identity_for
                and self.settings.identity.fallback_user_agent):
            fallback = self.settings.identity.fallback_user_agent
            retried = await self._get_once(url, check_robots=check_robots,
                                           etag=etag, last_modified=last_modified,
                                           user_agent=fallback)
            if retried.ok:
                # Sticky for the rest of the run. Per-domain data, not a
                # per-site branch: nothing here names a site.
                self._identity_for[domain] = fallback
                log.debug("%s identity fallback accepted", tag(domain))
                return retried
            # Remember the refusal too, so the next URL on this host does not
            # re-pay the extra request to learn the same answer.
            self._identity_for[domain] = self.settings.identity.user_agent
        return resp

    @staticmethod
    def _should_retry_identity(resp: Response) -> bool:
        """Only a refusal, and only one the fallback could plausibly change.

        Not a timeout, not a DNS failure, not a 5xx - none of those are the
        server declining who we are, and retrying them with a different string
        is knocking twice for no reason.
        """
        return resp.status == 403 or resp.wall is not None

    async def _get_once(
        self,
        url: str,
        *,
        check_robots: bool = True,
        etag: str | None = None,
        last_modified: str | None = None,
        user_agent: str | None = None,
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
            if not rules.allows(url, self.settings.identity.robots_agent):
                return Response(url=url, final_url=url, status=0, text="", headers={},
                                elapsed_s=0.0, error="robots-disallow")

        resp = await self._raw_get(url, paced=True, etag=etag,
                                   last_modified=last_modified,
                                   user_agent=user_agent)

        # The browser tier, placed HERE rather than in `get()` so it inherits
        # the circuit breaker and the robots check above it - the second of
        # those absolutely must not be reachable by another route. Gated on a
        # verdict the frontier supplied, so nothing in this file names a site.
        from .browser import should_escalate

        if self._escalate and should_escalate(
                resp,
                enabled=self.settings.browser_enabled,
                challenges_enabled=self.settings.browser_challenges_enabled,
                access=self._escalate.get(domain)):
            rendered = await self._render(url, domain)
            if rendered is not None and rendered.reached:
                return rendered
        return resp

    async def _render(self, url: str, domain: str) -> "Response | None":
        """Render one URL, under exactly the pacing an ordinary fetch takes."""
        from .browser import BrowserFetcher

        if self._browser is None:
            self._browser = BrowserFetcher(self.settings)
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        # Same context manager `_raw_get` uses, so this cannot be less polite
        # than the request it is replacing.
        async with self._paced(domain, parts.hostname or "", origin):
            return await self._browser.render(url, domain=domain)

