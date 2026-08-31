"""The optional browser transport.

A second way to obtain a page, behind the same `Response` the HTTP path
returns, so nothing downstream can tell the difference or needs to.

**Read the addressable set before turning this on.** Of 41 walls in the first
corpus run, 30 were "access denied" - a flat refusal that renders exactly the
same in Chrome. At most ~11 were genuine interstitials, and an honestly
identified browser will not clear most Cloudflare challenges either. The
defensible payoff is the `js_rendered` set: newsrooms that render with
JavaScript and have never challenged anyone. That is a rendering problem, not
an access problem, and no stance is at stake. Challenge hosts are a separate,
explicitly-dated decision (`SCRAPEV3_BROWSER_CHALLENGES`).

**Politeness is inherited structurally, not promised.** Every render runs
inside `PoliteFetcher._paced`, so it takes the global semaphore, the per-host
lock, the full delay with jitter and the per-IP cap - the same controls, in the
same order, as an ordinary fetch. A render is one paced request that happens to
take eight seconds instead of four hundred milliseconds. robots.txt is checked
upstream in `_get_once` and is never reached by a different route.

**It never solves a challenge.** The page is loaded and given a moment to
settle; if an interstitial is still there, that is recorded as a wall and the
crawl moves on. There is no CAPTCHA handling here and there should not be.

`nodriver` is imported lazily and its absence is not an error: a crawl on a
machine without Chrome behaves exactly as it does today.
"""

from __future__ import annotations

import asyncio
import time

from ..settings import Settings
from ..tracing import get as _get_logger, tag
from .client import Response, detect_wall

log = _get_logger(__name__)

# Flags that reduce cost without changing what the page is. Images stay ON
# deliberately: disabling them changes the fingerprint, some challenges depend
# on them, and the point of this tier is not to save 200KB.
_CHROME_FLAGS = (
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--no-first-run",
)


class BrowserUnavailable(RuntimeError):
    """nodriver is not installed, or no Chrome could be started."""


class BrowserFetcher:
    """One pooled browser, rendering one page at a time by default.

    Owned by `PoliteFetcher`, which is responsible for pacing. This class is
    responsible only for producing a `Response` or failing cleanly.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self._browser = None
        self._rendered = 0            # since the current browser started
        self._budget_used = 0         # for the whole run
        self._sem = asyncio.Semaphore(self.settings.browser.concurrency)
        self._start_lock = asyncio.Lock()
        # Set once, permanently, on the first failure to start. A capability
        # that is quietly absent is the failure mode this project exists to
        # eliminate, so it is logged once at WARNING and then never retried -
        # rather than paying a Chrome startup timeout on every article.
        self._unavailable: str | None = None

    @property
    def budget_remaining(self) -> int:
        return max(0, self.settings.browser.max_pages - self._budget_used)

    @property
    def unavailable(self) -> str | None:
        return self._unavailable

    async def _ensure_browser(self):
        """Start Chrome, or record permanently that we cannot."""
        if self._unavailable:
            raise BrowserUnavailable(self._unavailable)
        # Recycle before it leaks. Chrome grows; this is cheap and standard.
        if (self._browser is not None
                and self._rendered >= self.settings.browser.recycle_pages):
            await self.close()
        if self._browser is not None:
            return self._browser

        async with self._start_lock:
            if self._browser is not None:
                return self._browser
            try:
                import nodriver
            except ImportError as exc:
                self._unavailable = f"nodriver is not installed ({exc})"
                log.warning("browser tier disabled: %s. "
                            "Install with: pip install -e .[browser]",
                            self._unavailable)
                raise BrowserUnavailable(self._unavailable) from exc

            kwargs = {"headless": True, "browser_args": list(_CHROME_FLAGS)}
            if self.settings.browser.executable:
                kwargs["browser_executable_path"] = self.settings.browser.executable
            try:
                self._browser = await asyncio.wait_for(
                    nodriver.start(**kwargs),
                    timeout=self.settings.browser.timeout_s)
            except Exception as exc:                        # noqa: BLE001
                self._unavailable = f"{type(exc).__name__}: {exc}"
                log.warning("browser tier disabled: could not start Chrome (%s)",
                            self._unavailable)
                raise BrowserUnavailable(self._unavailable) from exc
            self._rendered = 0
            return self._browser

    async def render(self, url: str, *, domain: str = "") -> Response:
        """Load one URL in a real browser and return it as a `Response`.

        Never raises. Every failure path produces a `Response` the caller can
        treat exactly like any other, because the caller's job is to fall back
        to the HTTP result and carry on.
        """
        started = time.monotonic()
        if self._budget_used >= self.settings.browser.max_pages:
            return Response(url=url, final_url=url, status=0, text="", headers={},
                            elapsed_s=0.0, error="browser-budget-exhausted")
        async with self._sem:
            self._budget_used += 1
            try:
                browser = await self._ensure_browser()
            except BrowserUnavailable as exc:
                return Response(url=url, final_url=url, status=0, text="",
                                headers={}, elapsed_s=time.monotonic() - started,
                                error=f"browser-unavailable: {exc}")

            tab = None
            try:
                # A FRESH tab per URL, never reused across hosts: cookies and
                # storage leaking between publishers would be a correctness
                # and a privacy failure at the same time.
                tab = await asyncio.wait_for(
                    browser.get(url, new_tab=True),
                    timeout=self.settings.browser.timeout_s)
                html = await asyncio.wait_for(
                    tab.get_content(), timeout=self.settings.browser.timeout_s)
                final = url
                try:
                    final = await tab.evaluate("window.location.href") or url
                except Exception:                           # noqa: BLE001
                    pass
                self._rendered += 1
                # An interstitial that is STILL there after rendering is a wall
                # we did not clear, and saying so is the honest outcome. We do
                # not wait it out indefinitely and we never solve a CAPTCHA.
                return Response(url=url, final_url=final, status=200, text=html,
                                headers={}, elapsed_s=time.monotonic() - started,
                                wall=detect_wall(html))
            except asyncio.TimeoutError:
                log.debug("%s browser render timed out", tag(domain or url))
                return Response(url=url, final_url=url, status=0, text="",
                                headers={}, elapsed_s=time.monotonic() - started,
                                error="browser-timeout")
            except Exception as exc:                        # noqa: BLE001
                return Response(url=url, final_url=url, status=0, text="",
                                headers={}, elapsed_s=time.monotonic() - started,
                                error=f"browser-error: {type(exc).__name__}: {exc}")
            finally:
                if tab is not None:
                    try:
                        await tab.close()
                    except Exception:                       # noqa: BLE001
                        pass

    async def close(self) -> None:
        """Stop Chrome. An orphaned browser on a nightly cron is a real fault."""
        browser, self._browser = self._browser, None
        if browser is None:
            return
        try:
            stop = browser.stop()
            if asyncio.iscoroutine(stop):
                await asyncio.wait_for(stop, timeout=10)
        except Exception:                                   # noqa: BLE001
            pass


def should_escalate(
    resp: Response,
    *,
    enabled: bool,
    challenges_enabled: bool,
    access: str | None,
) -> bool:
    """Is a browser worth trying for this response? Pure; no I/O.

    Four gates, all of which must pass. Kept a plain function so every one of
    them is testable without Chrome, a network, or a frontier.
    """
    if not enabled:
        return False
    # Only a page we could not read. A working fetch is never re-fetched in a
    # browser - that would double the load on every healthy site on earth.
    if resp.reached:
        return False
    # `js_rendered` is the honest case: the site never refused us, it just does
    # not put its articles in the HTML. `challenge` is a site declining an
    # identified crawler, so it takes a second, explicit switch.
    if access == "js_rendered":
        return True
    if access == "challenge":
        return challenges_enabled
    return False
