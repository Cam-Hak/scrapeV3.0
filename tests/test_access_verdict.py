"""`needs_browser` was wired end-to-end and nothing ever wrote it.

The column existed on `domain_state`, `Frontier.release` accepted it,
`status.compose` read it, `classify` turned it into
`("blocked", "the page needs a browser to render its articles")`, it was
published to `agency_status`, and `clients/status.php` rendered it. The single
production caller never passed the argument, so in a real run it could only
ever be 0 - a whole pipeline reporting on a fact nobody supplied.

Two things were needed to fix it, and the second matters more than the first:

1. The fetcher had to expose what a host did to us. Its refusal counter was
   in-process and died with the fetcher; the frontier's own counter never
   learned *why*.

2. A boolean was the wrong shape. Of 41 walls in the first corpus run, 30 were
   "access denied" - a flat refusal that renders exactly the same in Chrome.
   Writing `needs_browser` for those would put a false sentence on 30
   publishers' rows and queue browser work certain to fail. Three outcomes need
   three words.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from scrapev3.fetch.browser import should_escalate
from scrapev3.fetch.client import PoliteFetcher
from scrapev3.frontier.models import ACCESS_VERDICT_TTL_DAYS, DomainRecord, utcnow
from scrapev3.frontier.store import SQLiteFrontier
from scrapev3.settings import Settings
from scrapev3.status import _worse_access, classify


def _fetcher_seeing(domain: str, *, wall=None, status=0, failures=1):
    f = PoliteFetcher(Settings.load())
    st = f._host_state(domain)
    st.consec_failures = failures
    st.last_wall = wall
    st.last_refused_status = status
    return f


class TestAChallengeIsNotARefusal:
    """The distinction the boolean could not carry."""

    def test_an_interstitial_is_a_challenge(self):
        for wall in ("just a moment...", "checking your browser",
                     "verify you are human", "js challenge (_cf_chl_opt)"):
            v = _fetcher_seeing("x.org", wall=wall).host_verdict("x.org")
            assert v.access == "challenge", wall

    def test_a_flat_denial_is_a_refusal(self):
        """30 of 41 walls. A browser renders this exactly the same."""
        for wall in ("access denied", "attention required! | cloudflare"):
            v = _fetcher_seeing("x.org", wall=wall).host_verdict("x.org")
            assert v.access == "refused", wall

    def test_a_bare_403_is_a_refusal(self):
        v = _fetcher_seeing("x.org", status=403).host_verdict("x.org")
        assert v.access == "refused"

    def test_our_own_resolver_is_neither(self):
        f = _fetcher_seeing("centcom.mil", status=403)
        f._dns_failures["www.centcom.mil"] = "gaierror"
        assert f.host_verdict("centcom.mil").access == "unresolved"

    def test_a_host_that_never_refused_has_no_verdict(self):
        f = PoliteFetcher(Settings.load())
        f._host_state("clean.org")
        assert f.host_verdict("clean.org") is None
        assert f.host_verdict("never-seen.org") is None

    def test_only_a_challenge_claims_to_need_a_browser(self):
        """The exact mapping `crawl.py` uses when releasing a domain."""
        for wall, expect in (("just a moment", True), ("access denied", False)):
            v = _fetcher_seeing("x.org", wall=wall).host_verdict("x.org")
            assert (v.access == "challenge") is expect, wall


class TestTheVerdictSurvivesTheFrontier:
    """It has to reach the website, which means it has to reach the store."""

    def test_access_round_trips(self, tmp_path):
        f = SQLiteFrontier(tmp_path / "f.sqlite")
        f.create_schema()
        f.upsert_sites([(1, "https://x.org/news", "x.org")])
        f.release("x.org", success=False, access="refused", needs_browser=False)
        rec = f.get("x.org")
        assert rec.access == "refused"
        assert rec.needs_browser is False
        f.close()

    def test_it_reaches_the_status_row(self, tmp_path):
        """`status_rows` is unpacked POSITIONALLY by `compose`, so a column
        appended in the wrong place silently shifts every later value."""
        f = SQLiteFrontier(tmp_path / "f.sqlite")
        f.create_schema()
        f.upsert_sites([(1, "https://x.org/news", "x.org")])
        f.release("x.org", success=False, access="challenge", needs_browser=True)
        row = f.status_rows()[0]
        assert row[8] == 1, "needs_browser"
        assert row[9] == "challenge", "access sits immediately after it"
        f.close()

    def test_a_migration_exists_for_an_existing_database(self, tmp_path):
        """`CREATE TABLE IF NOT EXISTS` never adds a column. The frontier had
        no migration path at all, so shipping this against a live 1,747-domain
        database would have made every SELECT fail with "no such column"."""
        import sqlite3

        path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE domain_state (domain TEXT PRIMARY KEY, a_id INT,"
            " newsroom_url TEXT, shard INT, enabled INT DEFAULT 1,"
            " next_allowed_at TEXT, leased_until TEXT, lease_owner TEXT,"
            " crawl_delay_s REAL DEFAULT 5.0, revisit_period_s INT DEFAULT 86400,"
            " consec_failures INT DEFAULT 0, discovery_method TEXT, feed_url TEXT,"
            " etag TEXT, last_modified TEXT, needs_browser INT DEFAULT 0,"
            " needs_browser_at TEXT, last_success_at TEXT, p50_body_len INT)")
        conn.commit()
        conn.close()

        f = SQLiteFrontier(path)          # must migrate, not explode
        f.create_schema()
        f.upsert_sites([(1, "https://x.org/news", "x.org")])
        f.release("x.org", success=False, access="refused")
        assert f.get("x.org").access == "refused"
        f.close()

    def test_the_verdict_goes_stale(self):
        """Sites remove challenges. `feed_absent` already paid for this."""
        fresh = DomainRecord(domain="x.org", a_id=1, newsroom_url="u", shard=0,
                             access="refused", needs_browser_at=utcnow())
        old = DomainRecord(domain="x.org", a_id=1, newsroom_url="u", shard=0,
                           access="refused",
                           needs_browser_at=utcnow()
                           - timedelta(days=ACCESS_VERDICT_TTL_DAYS + 1))
        none = DomainRecord(domain="x.org", a_id=1, newsroom_url="u", shard=0)
        assert fresh.access_verdict_is_fresh()
        assert not old.access_verdict_is_fresh()
        assert not none.access_verdict_is_fresh()


class TestTheGridSaysWhoseFaultItIs:
    """A publisher's row must not carry our defects, and vice versa."""

    BASE = dict(enabled=True, consec_failures=0, needs_browser=False,
                last_success_at=datetime(2026, 8, 30), last_article_at=None,
                articles=5, now=datetime(2026, 8, 31))

    def test_a_refusal_outranks_the_failure_counter(self):
        """A refusing host racks up consecutive failures too, so ordering these
        the other way reported "5 crawls in a row failed" - a statement about
        the publisher's reliability, for a decision their CDN made on purpose."""
        health, reason = classify(**{**self.BASE, "consec_failures": 5,
                                     "access": "refused"})
        assert health == "refused"
        assert "declining an identified crawler" in reason

    def test_our_resolver_is_named_as_ours(self):
        health, reason = classify(**{**self.BASE, "consec_failures": 9,
                                     "access": "unresolved"})
        assert health == "unresolved"
        assert "our resolver" in reason

    def test_a_challenge_still_reads_as_blocked(self):
        health, _ = classify(**{**self.BASE, "access": "challenge"})
        assert health == "blocked"

    def test_no_verdict_changes_nothing(self):
        assert classify(**self.BASE)[0] == "healthy"
        assert classify(**{**self.BASE, "consec_failures": 5})[0] == "failing"

    def test_the_new_words_have_severities(self):
        from scrapev3.status import severity_of

        assert severity_of("refused") == "warn", "their call, not our defect"
        assert severity_of("unresolved") == "error", "ours to fix"

    def test_an_agency_folds_its_targets_worst_first(self):
        assert _worse_access(None, "challenge") == "challenge"
        assert _worse_access("challenge", "refused") == "refused"
        assert _worse_access("refused", "challenge") == "refused"
        assert _worse_access("refused", None) == "refused"
        assert _worse_access(None, None) is None

    def test_the_ddl_and_migrations_still_agree(self):
        """CLAUDE.md's rule: anything appended after the first deployment must
        be in BOTH, or it exists only on new installs."""
        from scrapev3.status import _DDL, _MIGRATIONS

        assert "access" in _DDL
        assert "access" in {c for c, _ in _MIGRATIONS}


class TestTheBrowserTierIsPointedOnlyWhereItHelps:
    """`should_escalate` is a pure function so every gate is testable without
    Chrome, a network, or a frontier."""

    @staticmethod
    def _walled():
        from scrapev3.fetch.client import Response
        return Response(url="https://x.org/n", final_url="https://x.org/n",
                        status=200, text="", headers={}, elapsed_s=0.0,
                        wall="just a moment")

    def test_off_by_default(self):
        assert not should_escalate(self._walled(), enabled=False,
                             challenges_enabled=True, access="challenge")

    def test_a_js_rendered_site_is_the_honest_case(self):
        """It never refused us; it just does not put articles in the HTML."""
        assert should_escalate(self._walled(), enabled=True,
                         challenges_enabled=False, access="js_rendered")

    def test_a_challenge_needs_its_own_explicit_switch(self):
        """A challenge page IS a site declining an identified crawler, so
        pointing a browser at it must be a dated decision, not a default."""
        assert not should_escalate(self._walled(), enabled=True,
                             challenges_enabled=False, access="challenge")
        assert should_escalate(self._walled(), enabled=True,
                         challenges_enabled=True, access="challenge")

    def test_a_flat_refusal_is_never_escalated(self):
        """30 of 41 walls. Chrome renders "access denied" identically."""
        assert not should_escalate(self._walled(), enabled=True,
                             challenges_enabled=True, access="refused")

    def test_a_working_page_is_never_re_fetched_in_a_browser(self):
        """Otherwise every healthy site on earth gets fetched twice."""
        from scrapev3.fetch.client import Response
        good = Response(url="https://x.org/n", final_url="https://x.org/n",
                        status=200, text="<html/>", headers={}, elapsed_s=0.0)
        assert not should_escalate(good, enabled=True, challenges_enabled=True,
                             access="js_rendered")

    def test_a_host_with_no_verdict_is_never_escalated(self):
        assert not should_escalate(self._walled(), enabled=True,
                             challenges_enabled=True, access=None)
