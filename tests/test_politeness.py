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
