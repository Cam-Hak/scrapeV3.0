"""What we send, what we obey, and the gap between them.

55 of 1,747 audited targets returned 403. Holding the TLS fingerprint and every
other header constant and changing only the User-Agent:

    chrome131 TLS + TNSNewsBot UA  -> 403      defense.gov
    chrome131 TLS + Chrome UA      -> 200      weforum.org, michigan.gov

Appending our own token to a browser string returns 403 as well, so these WAFs
match on the presence of a bot token rather than on anything we did. Every one
of those sites publishes a robots.txt that returns `can_fetch = True` for us:
the publisher's stated policy permits the crawl and a CDN default overrides it.

The dangerous part of the fix is not the fallback. It is that `Protego.can_fetch`
keys entirely on the string it is handed, so pointing it at the User-Agent we
now sometimes change would silently swap which robots.txt group applies to us -
loosening the rules we obey as a side effect of a header. `robots_agent` exists
so that cannot happen, and the first class here is the regression test for it.
"""

from __future__ import annotations

import pytest
from curl_cffi.requests import BrowserType

from scrapev3.fetch.client import PoliteFetcher, Response
from scrapev3.settings import Settings

# `asyncio_mode = "auto"`, so async tests need no marker and sync ones here
# must not carry one.


def _resp(url: str, status: int, **kw) -> Response:
    return Response(url=url, final_url=url, status=status, text="", headers={},
                    elapsed_s=0.0, **kw)


class TestRobotsIsMatchedOnOurOwnToken:
    """The trap. Never the string we happen to be presenting."""

    async def test_robots_never_sees_the_sent_user_agent(self):
        seen: list[str] = []

        class _Fetcher(PoliteFetcher):
            async def robots_for(self, url):
                class _Rules:
                    def allows(_, u, ua):
                        seen.append(ua)
                        return True

                    def crawl_delay(_, ua):
                        seen.append(ua)
                        return None
                return _Rules()

            async def _raw_get(self, url, **kw):
                return _resp(url, 200)

        s = Settings.load()
        await _Fetcher(s).get("https://x.org/news")

        assert seen, "robots must actually have been consulted"
        assert all(ua == s.identity.robots_agent for ua in seen)
        assert s.identity.fallback_user_agent not in seen, \
            "presenting a browser string must never widen what we may fetch"

    def test_the_robots_token_is_ours_not_a_browser_string(self):
        s = Settings.load()
        assert "Mozilla" not in s.identity.robots_agent
        assert s.identity.robots_agent == "TNSNewsBot"

    def test_the_primary_identity_is_still_the_honest_one(self):
        """We lead with the bot UA on every host. The fallback is a repair
        path, not the default - which is also what keeps Cloudflare's Verified
        Bot programme reachable."""
        s = Settings.load()
        assert s.identity.user_agent.startswith("TNSNewsBot")

    def test_the_contact_header_is_sent_under_both_identities(self):
        """A publisher must be able to find out who we are either way."""
        s = Settings.load()
        primary = s.identity.headers()
        fallback = s.identity.headers(s.identity.fallback_user_agent)
        assert primary["From"] == fallback["From"] == s.identity.contact_email
        assert fallback["User-Agent"] == s.identity.fallback_user_agent


class TestTheFallbackIsGatedOnRefusalOnly:
    """One extra request, only on a refusal, only once per host."""

    @staticmethod
    def _spy(first: Response):
        """A fetcher whose first answer is `first` and whose retry succeeds."""
        calls: list[str | None] = []

        class _Fetcher(PoliteFetcher):
            async def _get_once(self, url, *, user_agent=None, **kw):
                calls.append(user_agent)
                if user_agent and user_agent.startswith("Mozilla"):
                    return _resp(url, 200)
                return first
        return _Fetcher, calls

    async def test_a_403_retries_once_with_the_fallback(self):
        F, calls = self._spy(_resp("https://defense.gov/news", 403))
        resp = await F(Settings.load()).get("https://defense.gov/news")
        assert resp.ok
        assert len(calls) == 2
        assert calls[0] is None, "the honest identity goes first, always"
        assert calls[1].startswith("Mozilla")

    async def test_a_wall_retries_too(self):
        F, calls = self._spy(_resp("https://x.org/news", 200, wall="access denied"))
        resp = await F(Settings.load()).get("https://x.org/news")
        assert resp.ok and len(calls) == 2

    @pytest.mark.parametrize("first", [
        _resp("https://x.org/n", 404),
        _resp("https://x.org/n", 500),
        _resp("https://x.org/n", 0, error="Timeout: timed out"),
        _resp("https://x.org/n", 0, error="robots-disallow"),
    ])
    async def test_nothing_else_triggers_it(self, first):
        """A 404, a 5xx, a timeout and a robots refusal are all answers about
        something other than who we are. Knocking twice would be knocking for
        no reason."""
        F, calls = self._spy(first)
        await F(Settings.load()).get(first.url)
        assert len(calls) == 1

    async def test_the_verdict_is_sticky_for_the_whole_host(self):
        """A refusing host costs ONE extra request per run, not one per URL."""
        F, calls = self._spy(_resp("https://defense.gov/a", 403))
        f = F(Settings.load())
        await f.get("https://defense.gov/a")
        await f.get("https://defense.gov/b")
        await f.get("https://defense.gov/c")
        assert len(calls) == 4, "1 probe + 1 fallback, then 2 direct"
        assert all(c.startswith("Mozilla") for c in calls[1:])

    async def test_a_host_that_refuses_both_is_not_re_probed(self):
        """Learning "neither works" is worth remembering too."""
        calls: list[str | None] = []

        class _Always403(PoliteFetcher):
            async def _get_once(self, url, *, user_agent=None, **kw):
                calls.append(user_agent)
                return _resp(url, 403)

        f = _Always403(Settings.load())
        await f.get("https://x.org/a")
        await f.get("https://x.org/b")
        assert len(calls) == 3, "2 on the first URL, 1 on the second"

    async def test_disabling_the_fallback_leaves_one_request(self):
        import dataclasses

        s = Settings.load()
        s = dataclasses.replace(
            s, identity=dataclasses.replace(s.identity, fallback_user_agent=""))
        F, calls = self._spy(_resp("https://x.org/n", 403))
        await F(s).get("https://x.org/n")
        assert len(calls) == 1


class TestThePinIsNotJustAComment:
    """`impersonate` carried "re-pinned quarterly" in a comment while sitting
    26 releases behind. A comment is not an invariant."""

    def test_the_configured_target_exists_in_the_installed_curl_cffi(self):
        available = {b.value for b in BrowserType}
        assert Settings.load().identity.impersonate in available

    def test_it_is_not_wildly_behind_what_is_available(self):
        """Impersonating a Chrome long out of support is its own anomaly."""
        available = sorted(
            int(b.value[len("chrome"):]) for b in BrowserType
            if b.value.startswith("chrome") and b.value[len("chrome"):].isdigit())
        pinned = Settings.load().identity.impersonate
        if not (pinned.startswith("chrome") and pinned[6:].isdigit()):
            pytest.skip("not a plain chrome pin")
        assert int(pinned[6:]) >= available[-1] - 12, (
            f"pinned {pinned}, newest available chrome{available[-1]}")
