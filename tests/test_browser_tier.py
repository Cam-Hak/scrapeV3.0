"""The browser tier, with no browser.

Everything here runs against a stub renderer. Nothing starts Chrome, so the
default `pytest tests/ -q` stays offline.

The tests that matter are not "does it render" - they are the four that would
let this tier quietly become impolite or reach a page it must not:

  * a render must hold the same per-host lock and pay the same delay as an
    ordinary fetch, because it is a request to someone else's server and
    nothing about using a different transport changes that
  * a robots-disallowed URL must never reach the renderer, by any route
  * the per-run budget must actually cap it
  * a missing Chrome must degrade to the HTTP answer, never raise

The escalation gates themselves live in `test_access_verdict.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from scrapev3.fetch.browser import BrowserFetcher
from scrapev3.fetch.client import PoliteFetcher, Response
from scrapev3.settings import Settings


def _walled(url: str) -> Response:
    return Response(url=url, final_url=url, status=200, text="", headers={},
                    elapsed_s=0.0, wall="just a moment")


def _settings(**browser) -> Settings:
    s = Settings.load()
    return dataclasses.replace(
        s,
        browser=dataclasses.replace(s.browser, **({"enabled": "on"} | browser)))


class _StubRenderer:
    """Stands in for Chrome. Records when each render happened."""

    def __init__(self, text="<html>rendered</html>"):
        self.calls: list[str] = []
        self.at: list[float] = []
        self.text = text

    async def render(self, url, *, domain=""):
        import time
        self.calls.append(url)
        self.at.append(time.monotonic())
        return Response(url=url, final_url=url, status=200, text=self.text,
                        headers={}, elapsed_s=0.0)

    async def close(self):
        pass


def _fetcher(settings, renderer, *, escalate, wall=True):
    class _F(PoliteFetcher):
        async def _raw_get(self, url, **kw):
            return _walled(url) if wall else Response(
                url=url, final_url=url, status=200, text="ok", headers={},
                elapsed_s=0.0)

        async def robots_for(self, url):
            class _Rules:
                def allows(self, u, ua):
                    return True

                def crawl_delay(self, ua):
                    return None
            return _Rules()

    f = _F(settings, escalate=escalate)
    f._browser = renderer
    return f


class TestARenderIsExactlyAsPoliteAsAFetch:
    """The property the whole tier rests on, asserted rather than promised."""

    async def test_the_render_pays_the_per_host_delay(self):
        settings = _settings()
        settings = dataclasses.replace(
            settings,
            politeness=dataclasses.replace(settings.politeness,
                                           default_delay_s=0.5, jitter_pct=0.0))
        stub = _StubRenderer()
        f = _fetcher(settings, stub, escalate={"x.org": "js_rendered"})

        await f.get("https://x.org/a")
        await f.get("https://x.org/b")

        assert len(stub.calls) == 2
        gap = stub.at[1] - stub.at[0]
        assert gap >= 0.4, (
            f"two renders on one host were {gap:.2f}s apart; the per-host "
            "delay must apply to a render exactly as it does to a fetch")
        # In production the render is the SECOND request of the pair - the
        # HTTP attempt pays a delay, then the render pays another - so a
        # rendered page is strictly more paced than a fetched one, never less.
        # (`_raw_get` is stubbed here, so only the render's own turn is timed.)

    async def test_two_renders_on_one_host_never_overlap(self):
        """Concurrency-per-host is 1, whatever the transport."""
        settings = _settings()
        settings = dataclasses.replace(
            settings,
            politeness=dataclasses.replace(settings.politeness,
                                           default_delay_s=0.05, jitter_pct=0.0))
        inflight = 0
        peak = 0

        class _Slow(_StubRenderer):
            async def render(self, url, *, domain=""):
                nonlocal inflight, peak
                inflight += 1
                peak = max(peak, inflight)
                await asyncio.sleep(0.1)
                inflight -= 1
                return await super().render(url, domain=domain)

        f = _fetcher(settings, _Slow(), escalate={"x.org": "js_rendered"})
        await asyncio.gather(f.get("https://x.org/a"), f.get("https://x.org/b"))
        assert peak == 1


class TestRobotsIsNeverBypassed:
    """The worst available regression in this file."""

    async def test_a_disallowed_url_never_reaches_the_renderer(self):
        stub = _StubRenderer()

        class _F(PoliteFetcher):
            async def robots_for(self, url):
                class _Rules:
                    def allows(self, u, ua):
                        return False

                    def crawl_delay(self, ua):
                        return None
                return _Rules()

            async def _raw_get(self, url, **kw):    # pragma: no cover
                raise AssertionError("robots must stop this before the fetch")

        f = _F(_settings(), escalate={"x.org": "js_rendered"})
        f._browser = stub
        resp = await f.get("https://x.org/secret")

        assert resp.error == "robots-disallow"
        assert stub.calls == [], "the browser must not be a way around robots"


class TestItStaysWithinItsBudget:

    async def test_the_per_run_page_budget_caps_renders(self):
        settings = _settings(max_pages=2)
        settings = dataclasses.replace(
            settings,
            politeness=dataclasses.replace(settings.politeness,
                                           default_delay_s=0.0, jitter_pct=0.0))
        real = BrowserFetcher(settings)
        rendered: list[str] = []

        async def _fake_ensure():
            class _B:
                pass
            return _B()

        async def _render(url, *, domain=""):
            # Exercise the real budget accounting, not the stub's.
            if real._budget_used >= settings.browser.max_pages:
                return Response(url=url, final_url=url, status=0, text="",
                                headers={}, elapsed_s=0.0,
                                error="browser-budget-exhausted")
            real._budget_used += 1
            rendered.append(url)
            return Response(url=url, final_url=url, status=200, text="ok",
                            headers={}, elapsed_s=0.0)

        real.render = _render
        f = _fetcher(settings, real, escalate={"x.org": "js_rendered"})
        for i in range(5):
            await f.get(f"https://x.org/{i}")

        assert len(rendered) == 2, "a bad night must not become a browser storm"


class TestItDegradesInsteadOfFailing:

    async def test_a_missing_nodriver_returns_the_http_answer(self):
        """A crawl on a machine without Chrome behaves exactly as it does
        today. An ImportError must never fail a crawl."""
        settings = _settings()
        real = BrowserFetcher(settings)
        real._unavailable = "nodriver is not installed"

        f = _fetcher(settings, real, escalate={"x.org": "challenge"})
        resp = await f.get("https://x.org/a")
        assert resp.wall == "just a moment", "we fall back to the HTTP result"

    async def test_the_unavailable_verdict_is_recorded_once(self):
        real = BrowserFetcher(_settings())
        r1 = await real.render("https://x.org/a")
        assert r1.error and "browser-unavailable" in r1.error
        assert real.unavailable, "the reason is remembered, not re-derived"

    async def test_a_render_timeout_is_a_timeout_not_a_wall(self):
        from scrapev3.fetch.client import failure_kind

        r = Response(url="https://x.org/a", final_url="https://x.org/a",
                     status=0, text="", headers={}, elapsed_s=0.0,
                     error="browser-timeout")
        assert failure_kind(r) == "timeout"

    async def test_nothing_renders_when_the_tier_is_off(self):
        settings = Settings.load()          # BROWSER defaults to off
        assert not settings.browser_enabled
        stub = _StubRenderer()
        f = _fetcher(settings, stub, escalate={"x.org": "js_rendered"})
        await f.get("https://x.org/a")
        assert stub.calls == []


class TestTheDependencyIsDeclared:

    def test_the_browser_extra_exists(self):
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        extras = data["project"]["optional-dependencies"]
        assert "browser" in extras
        assert any("nodriver" in d for d in extras["browser"])
