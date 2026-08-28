"""Politeness verification against a real local HTTP server.

The plan makes three explicit assertions that have to be *proven*, not assumed,
because "we must never look like a DoS source" is a stated project requirement:

  1. No single registrable domain ever has two overlapping in-flight requests.
  2. The observed minimum gap between requests to one host >= its crawl delay.
  3. Jitter is actually applied (gaps are not identical).

This spins up a throwaway HTTP server that records the arrival and completion
timestamp of every request, then inspects the record.
"""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scrapev3.fetch import PoliteFetcher, detect_wall
from scrapev3.settings import Settings

# (path, arrived_at, finished_at)
ARRIVALS: list[tuple[str, float, float]] = []
_LOCK = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                    # noqa: N802
        arrived = time.monotonic()
        # Hold the connection briefly so genuine overlap would be detectable.
        time.sleep(0.05)
        if self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            ctype = "text/plain"
        else:
            body = b"<html><head><title>Story</title></head><body><p>text</p></body></html>"
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with _LOCK:
            ARRIVALS.append((self.path, arrived, time.monotonic()))

    def log_message(self, *args):                        # silence stderr noise
        return


@pytest.fixture
def server():
    ARRIVALS.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def fast_settings(monkeypatch):
    """Same logic, compressed timescale so the suite stays quick."""
    monkeypatch.setenv("SCRAPEV3_DEFAULT_DELAY_S", "0.4")
    monkeypatch.setenv("SCRAPEV3_JITTER_PCT", "0.3")
    monkeypatch.setenv("SCRAPEV3_GLOBAL_CONCURRENCY", "16")
    return Settings()


def _article_gaps(paths_prefix: str = "/a") -> list[float]:
    hits = sorted(a for p, a, _ in ARRIVALS if p.startswith(paths_prefix))
    return [b - a for a, b in zip(hits, hits[1:])]


async def test_never_two_overlapping_requests_to_one_host(server, fast_settings):
    """The core politeness invariant: concurrency per host is 1."""
    async with PoliteFetcher(fast_settings) as f:
        await asyncio.gather(*(f.get(f"{server}/a{i}") for i in range(8)))

    spans = sorted((a, b) for p, a, b in ARRIVALS if p.startswith("/a"))
    for (start_a, end_a), (start_b, _) in zip(spans, spans[1:]):
        assert start_b >= end_a, (
            f"overlapping requests to one host: {start_b} started before {end_a} finished"
        )


async def test_minimum_gap_respects_delay(server, fast_settings):
    """No gap may fall below delay minus the jitter band."""
    async with PoliteFetcher(fast_settings) as f:
        await asyncio.gather(*(f.get(f"{server}/a{i}") for i in range(8)))

    gaps = _article_gaps()
    assert gaps, "no requests recorded"
    floor = fast_settings.politeness.default_delay_s * (1 - fast_settings.politeness.jitter_pct)
    assert min(gaps) >= floor * 0.9, f"gap {min(gaps):.3f}s below floor {floor:.3f}s"


async def test_jitter_is_actually_applied(server, fast_settings):
    """Evenly-spaced requests are a detectable periodic signature."""
    async with PoliteFetcher(fast_settings) as f:
        await asyncio.gather(*(f.get(f"{server}/a{i}") for i in range(10)))

    gaps = _article_gaps()
    assert len(set(round(g, 2) for g in gaps)) > 1, "gaps are identical - jitter not applied"


async def test_robots_disallow_is_honored(server, monkeypatch, fast_settings):
    """A disallowed path must never reach the network."""
    async with PoliteFetcher(fast_settings) as f:
        rules = await f.robots_for(f"{server}/")
        # Swap in a restrictive policy, then confirm we refuse to fetch.
        from scrapev3.fetch.robots import parse_robots
        f._robots[server] = parse_robots(server, "User-agent: *\nDisallow: /private\n", 200)

        resp = await f.get(f"{server}/private/secret")
        assert resp.error == "robots-disallow"
        assert not any(p.startswith("/private") for p, _, _ in ARRIVALS)

        allowed = await f.get(f"{server}/public/story")
        assert allowed.ok
    assert rules.status == 200


async def test_crawl_delay_raises_but_never_lowers_our_floor(server, fast_settings):
    """Crawl-delay is a floor. A tiny declared value must not speed us up."""
    from scrapev3.fetch.robots import parse_robots

    async with PoliteFetcher(fast_settings) as f:
        f._robots[server] = parse_robots(
            server, "User-agent: *\nAllow: /\nCrawl-delay: 0.01\n", 200)
        effective = f._effective_delay("127.0.0.1", f._robots[server])
        assert effective >= fast_settings.politeness.default_delay_s

        f._hosts.clear()
        f._robots[server] = parse_robots(
            server, "User-agent: *\nAllow: /\nCrawl-delay: 30\n", 200)
        effective = f._effective_delay("127.0.0.1", f._robots[server])
        assert effective == 30.0


class TestWallDetection:
    """A bot wall returns HTTP 200 with plausible HTML, so status codes miss it."""

    def test_detects_cloudflare_interstitial(self):
        html = "<html><head><title>Just a moment...</title></head><body></body></html>"
        assert detect_wall(html) is not None

    def test_detects_access_denied(self):
        assert detect_wall("<html><title>Access Denied</title></html>") is not None

    def test_ignores_normal_article(self):
        html = "<html><head><title>Mayor announces transit plan</title></head></html>"
        assert detect_wall(html) is None

    def test_handles_missing_title(self):
        assert detect_wall("<html><body>hi</body></html>") is None

    def test_handles_empty(self):
        assert detect_wall("") is None

    def test_title_with_attributes(self):
        html = '<html><head><title data-x="1">Attention Required!</title></head></html>'
        assert detect_wall(html) is not None


class TestPerIpConcurrency:
    """Secondary constraint, added from Phase 1 survey evidence.

    28.3% of surveyed domains shared an IP with another domain (largest cluster
    28), on managed-hosting edges like WP Engine and Pantheon. Per-domain pacing
    alone would let all 28 burst at one edge simultaneously, since each is a
    different registrable domain and therefore a different host lock.

    These exercise the IP semaphore directly. Driving it through real HTTP would
    be vacuous: every request would go to one hostname, so the per-host lock
    (concurrency 1) would serialise them and the IP cap would never bind.
    """

    def test_same_ip_shares_one_semaphore(self, fast_settings):
        f = PoliteFetcher(fast_settings)
        a = f._ip_sem("203.0.113.9")
        b = f._ip_sem("203.0.113.9")
        assert a is b, "distinct domains on one IP must share a semaphore"

    def test_distinct_ips_get_distinct_semaphores(self, fast_settings):
        f = PoliteFetcher(fast_settings)
        assert f._ip_sem("203.0.113.1") is not f._ip_sem("203.0.113.2")

    async def test_semaphore_caps_concurrency_at_the_configured_limit(self, monkeypatch):
        monkeypatch.setenv("SCRAPEV3_MAX_CONCURRENCY_PER_IP", "4")
        f = PoliteFetcher(Settings())

        in_flight = 0
        peak = 0

        async def one_request():
            nonlocal in_flight, peak
            async with f._ip_sem("203.0.113.9"):
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1

        # 28 concurrent, matching the largest real cluster in the survey.
        await asyncio.gather(*(one_request() for _ in range(28)))
        assert peak == 4, f"peak concurrency to one IP was {peak}, cap was 4"

    async def test_all_requests_still_complete(self, monkeypatch):
        """Capping must throttle, never drop."""
        monkeypatch.setenv("SCRAPEV3_MAX_CONCURRENCY_PER_IP", "2")
        f = PoliteFetcher(Settings())
        done = []

        async def one(i):
            async with f._ip_sem("203.0.113.9"):
                await asyncio.sleep(0.01)
                done.append(i)

        await asyncio.gather(*(one(i) for i in range(20)))
        assert sorted(done) == list(range(20))

    async def test_resolution_is_cached(self, fast_settings):
        """One DNS lookup per hostname, off the request critical path."""
        f = PoliteFetcher(fast_settings)
        calls = []
        real = asyncio.get_running_loop().getaddrinfo

        async def counting(host, *a, **kw):
            calls.append(host)
            return await real(host, *a, **kw)

        asyncio.get_running_loop().getaddrinfo = counting
        try:
            first = await f._resolve("localhost")
            second = await f._resolve("localhost")
        finally:
            asyncio.get_running_loop().getaddrinfo = real

        assert first == second
        assert len(calls) == 1, f"resolved {len(calls)} times, expected 1"


class TestWwwFallback:
    """Canonicalisation strips `www.`, which is right for identity and is not
    an assertion that the apex host serves anything.

    Two of ten domains in one run were unreachable for exactly that reason:
    ardian.com has no A record at all, and escardio.org resolves to a different
    host than www.escardio.org and fails TLS there. Both work once the
    conventional host is restored.
    """

    def test_subdomains_get_the_variant_too(self):
        """law.georgetown.edu does not resolve; www.law.georgetown.edu does.
        Restricting this to apex hosts left that target permanently dead, so
        the rule is "any host that is not already www", and the DNS failure
        does the deciding."""
        from scrapev3.fetch.client import _with_www

        assert _with_www("https://ardian.com/news") == "https://www.ardian.com/news"
        assert _with_www("https://bma.org.uk/news") == "https://www.bma.org.uk/news"
        assert _with_www("https://law.georgetown.edu/news") ==             "https://www.law.georgetown.edu/news"
        # Already www: there is no further variant to try.
        assert _with_www("https://www.ardian.com/news") is None

    def test_the_query_and_path_survive_the_rewrite(self):
        from scrapev3.fetch.client import _with_www

        assert _with_www("https://escardio.org/news/press?page=1&q=") == \
            "https://www.escardio.org/news/press?page=1&q="

    async def test_a_dns_failure_retries_on_www_and_a_404_does_not(self):
        """The retry is gated on "no server ever answered". A real 404 is a
        real answer about a real host, and re-requesting it would spend a
        second paced request to learn nothing."""
        attempts: list[str] = []

        class _Fetcher(PoliteFetcher):
            async def _get_once(self, url, **kw):
                attempts.append(url)
                from scrapev3.fetch.client import Response

                if url.startswith("https://www."):
                    return Response(url=url, final_url=url, status=200, text="ok",
                                    headers={}, elapsed_s=0.0)
                return Response(url=url, final_url=url, status=0, text="", headers={},
                                elapsed_s=0.0,
                                error="DNSError: curl: (6) Could not resolve host")

        f = _Fetcher(Settings.load())
        resp = await f.get("https://ardian.com/news")
        assert resp.ok and resp.url == "https://www.ardian.com/news"
        assert len(attempts) == 2

        attempts.clear()

        class _NotFound(PoliteFetcher):
            async def _get_once(self, url, **kw):
                attempts.append(url)
                from scrapev3.fetch.client import Response

                return Response(url=url, final_url=url, status=404, text="",
                                headers={}, elapsed_s=0.0)

        resp = await _NotFound(Settings.load()).get("https://ardian.com/gone")
        assert not resp.ok
        assert len(attempts) == 1, "a 404 must not trigger the www retry"


class TestRefusalsStopTheKnocking:
    """A run of 403s is a verdict, not a transient fault.

    news.csub.edu served a Cloudflare 403 to fourteen consecutive article
    fetches in one pass - each one waiting out the full per-host delay first,
    and each one certain to be refused. The circuit breaker existed but only
    opened on 429/503, so refusals were paid for in full on every run.
    """

    def _state_after(self, n_refusals: int, status: int = 403):
        from scrapev3.fetch.client import Response, _HostState

        f = PoliteFetcher(Settings.load())
        state = _HostState()
        for _ in range(n_refusals):
            f._apply_backoff(state, Response(url="https://x.test/a", final_url="https://x.test/a",
                                             status=status, text="", headers={}, elapsed_s=0.1))
        return state

    def test_the_circuit_opens_after_the_threshold(self):
        import time as _t

        state = self._state_after(Settings.load().politeness.max_consec_refusals)
        assert state.blocked_until > _t.monotonic(), "host should be left alone"

    def test_one_refusal_does_not_open_it(self):
        """A single 403 is ordinary - a robots-protected path, a stale link.
        Only a run of them says the host is refusing us."""
        state = self._state_after(1)
        assert state.blocked_until == 0.0

    def test_a_success_clears_the_streak(self):
        from scrapev3.fetch.client import Response

        f = PoliteFetcher(Settings.load())
        state = self._state_after(2)
        assert state.consec_failures == 2
        f._apply_backoff(state, Response(url="https://x.test/a", final_url="https://x.test/a",
                                         status=200, text="ok", headers={}, elapsed_s=0.1))
        assert state.consec_failures == 0


class TestRedirectsAreBounded:
    """Redirects are followed inside curl, so they never reach _wait_turn.

    ersnet.org 301s /news-and-features/news/ to itself forever. At curl's
    default of 30 hops, ten article fetches became roughly three hundred
    back-to-back unpaced requests to one host - a politeness failure that no
    per-host delay could see, because the delay is applied per get() call.
    """

    def test_the_cap_is_well_below_curls_default(self):
        pol = Settings.load().politeness
        assert pol.max_redirects <= 10, "a real article is never ten hops away"
        assert pol.max_redirects >= 1, "some sites legitimately redirect once"

    def test_the_session_is_configured_with_it(self):
        """Pins the wiring, not just the value - the setting is worthless if it
        never reaches the session."""
        import inspect

        from scrapev3.fetch import client

        src = inspect.getsource(client.PoliteFetcher.__aenter__)
        assert "max_redirects" in src


class TestTheBreakerSaysWhyItOpened:
    """A breaker that hides its cause replaces one silent failure with another.

    One run reported "circuit-open: host backing off x11" and nothing else: the
    failures that tripped it happened during discovery, which does not feed the
    crawl's failure tally, so the article fetches that followed only ever saw
    an open circuit. The reason now travels with it.
    """

    def _tripped(self, status=403, n=None):
        from scrapev3.fetch.client import Response, _HostState

        pol = Settings.load().politeness
        f = PoliteFetcher(Settings.load())
        state = _HostState()
        for _ in range(n or pol.max_consec_refusals):
            f._apply_backoff(state, Response(url="https://x.test/a", final_url="https://x.test/a",
                                             status=status, text="", headers={}, elapsed_s=0.1))
        return state

    def test_a_refusal_streak_records_its_cause(self):
        state = self._tripped()
        assert state.blocked_reason and "403" in state.blocked_reason

    def test_a_503_records_its_cause(self):
        state = self._tripped(status=503, n=1)
        assert state.blocked_reason == "HTTP 503"

    def test_recovery_clears_the_reason(self):
        from scrapev3.fetch.client import Response

        f = PoliteFetcher(Settings.load())
        state = self._tripped()
        assert state.blocked_reason is not None
        f._apply_backoff(state, Response(url="https://x.test/a", final_url="https://x.test/a",
                                         status=200, text="ok", headers={}, elapsed_s=0.1))
        assert state.blocked_reason is None


class TestJsChallengeWalls:
    """A wall arrives two ways, and both answer HTTP 200.

    The rendered kind says so in its <title>. The other is a bare script that
    would run in a browser: justice.gov serves 2.6 KB containing
    triggerInterstitialChallenge() and no text at all. That read as
    "needs_browser: low text yield", so every article was queued for a browser
    that would meet the same challenge - and because it was never classified as
    a refusal, the host was never backed off from either.
    """

    CHALLENGE = ('<html><head></head><body>&nbsp;<script>var i = 1787874706;'
                 ' function triggerInterstitialChallenge() {'
                 ' var xhr = new XMLHttpRequest(); }</script></body></html>')

    def test_a_script_only_challenge_is_a_wall(self):
        assert detect_wall(self.CHALLENGE)

    def test_the_marker_names_the_vendor(self):
        assert "triggerinterstitialchallenge" in detect_wall(self.CHALLENGE)

    def test_an_ordinary_short_page_is_not_a_wall(self):
        assert detect_wall("<html><title>News</title><body>"
                           + "word " * 200 + "</body></html>") is None

    def test_a_real_article_mentioning_a_vendor_is_not_a_wall(self):
        """The size gate is what makes the marker list safe: an article about
        bot protection is still an article, and it is never 8 KB."""
        page = ("<html><title>How the challenge-platform works</title><body>"
                + "word " * 3000 + "</body></html>")
        assert len(page) > 8192
        assert detect_wall(page) is None

    def test_a_wall_is_not_ok_even_at_200(self):
        from scrapev3.fetch.client import Response

        r = Response(url="https://x.test/a", final_url="https://x.test/a", status=200,
                     text=self.CHALLENGE, headers={}, elapsed_s=0.1,
                     wall=detect_wall(self.CHALLENGE))
        assert not r.ok, "200 plus a challenge is not a successful fetch"
