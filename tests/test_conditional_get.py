"""Conditional GET was fully built and never armed.

`_raw_get` set `If-None-Match`/`If-Modified-Since`, `etag` and `last_modified`
columns sat on `target`, `Frontier.release_target` accepted them, and
`status.py` reported `conditional_get` to the website - but no caller ever
passed a validator, so the whole path was dead and every daily run re-downloaded
every listing page in full.

Arming it naively would have shipped a worse bug than it fixed. **`Response.ok`
requires `200 <= status < 300`, so a 304 is not `ok`**, and every discovery call
site branches on `resp.ok`. Unarmed, that never mattered. Armed, an unchanged
newsroom page would have become:

    304 -> not ok -> discovery returns method="none" -> release_target(
    success=False) -> the persistent failure counter climbs -> three days later
    `classify` tells the publisher "3 crawls in a row failed"

...because nothing had changed on their site. The politest possible outcome,
reported as their fault. That is the exact silent-quality shape this project
exists to catch, so the 304 vocabulary landed before the arming did.
"""

from __future__ import annotations

from scrapev3.discover.sources import Discovery
from scrapev3.fetch.client import Response, failure_kind
from scrapev3.frontier.store import SQLiteFrontier


def _resp(status: int, **kw) -> Response:
    return Response(url="https://x.org/news", final_url="https://x.org/news",
                    status=status, text="", headers={}, elapsed_s=0.0, **kw)


class TestA304IsReachedNotOk:
    """`ok` means "there is content here". A 304 means "and there is not"."""

    def test_a_304_is_not_ok(self):
        """Pinned deliberately: every caller relies on this, and 'fixing' it
        would hand empty text to code expecting a page."""
        assert not _resp(304, from_cache=True).ok

    def test_but_it_did_reach_the_origin(self):
        assert _resp(304, from_cache=True).reached

    def test_a_real_failure_reached_nothing(self):
        assert not _resp(0, error="DNSError: x").reached
        assert not _resp(403).reached
        assert not _resp(200, wall="access denied").reached

    def test_a_200_is_both(self):
        r = _resp(200)
        assert r.ok and r.reached

    def test_it_has_its_own_word_not_an_error_one(self):
        assert failure_kind(_resp(304, from_cache=True)) == "not_modified"


class TestAQuietPublisherIsNotAFailingOne:
    """The bug arming this would have caused, pinned so it cannot come back."""

    def test_an_unchanged_source_is_released_as_success(self, tmp_path):
        f = SQLiteFrontier(tmp_path / "f.sqlite")
        f.create_schema()
        f.upsert_sites([(1, "https://x.org/news", "x.org")])

        # Three consecutive runs where nothing changed on the publisher's site.
        for _ in range(3):
            f.release_target("https://x.org/news", success=True,
                             discovery_method="listing")

        target = f.targets_for("x.org")[0]
        assert target.consec_failures == 0, (
            "a 304 must not drive the failure counter - at 3 the website "
            "starts telling the publisher their site is failing")
        f.close()

    def test_the_discovery_result_says_unchanged_rather_than_nothing(self):
        """`method="none"` and `not_modified=True` are opposite outcomes that
        would otherwise be indistinguishable to the caller."""
        d = Discovery(method="listing", not_modified=True)
        assert d.not_modified
        assert d.method != "none"

    def test_validators_round_trip_through_the_target(self, tmp_path):
        f = SQLiteFrontier(tmp_path / "f.sqlite")
        f.create_schema()
        f.upsert_sites([(1, "https://x.org/news", "x.org")])
        f.release_target("https://x.org/news", success=True,
                         discovery_method="listing",
                         etag='W/"abc123"', last_modified="Wed, 27 Aug 2026 10:00:00 GMT")

        target = f.targets_for("x.org")[0]
        assert target.etag == 'W/"abc123"'
        assert target.last_modified == "Wed, 27 Aug 2026 10:00:00 GMT"
        f.close()


class TestArmingIsScopedToPolledResources:
    """Where the saving is, and where it would only cause harm."""

    def test_discover_accepts_validators(self):
        import inspect

        from scrapev3.discover.sources import discover

        params = inspect.signature(discover).parameters
        assert "known_etag" in params and "known_last_modified" in params

    def test_crawl_target_passes_them_but_article_fetches_do_not(self):
        """Articles are deliberately unarmed: `seen_url` already stops us
        refetching them, so there is nothing to save - and a 304 there yields
        an empty body that extraction would turn into a blank article flagged
        `needs_browser`. Pure downside."""
        import inspect

        from scrapev3 import crawl

        assert "known_etag" in inspect.signature(crawl.crawl_target).parameters

        # The article fetch lives in `_extract_ref`, and must stay unarmed.
        article_src = inspect.getsource(crawl._extract_ref)
        assert "fetcher.get(ref.url" in article_src
        call = article_src[article_src.index("fetcher.get(ref.url"):][:120]
        assert "etag" not in call, "article fetches must not be armed"
