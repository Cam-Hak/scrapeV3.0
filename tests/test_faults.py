"""Ranking failures by what is worth fixing first.

The vocabulary already existed and was already closed - `failure_kind` and the
`access` verdict landed with the corpus-wide audit. What did not exist was a
reason to look at one line before another, so a run's output was a histogram
sorted by count, and count is the wrong axis: one site 404ing forty URLs and
twenty sites failing once each are the same number and completely different
problems.

Three things are worth pinning here. That the vocabulary constants actually
cover the function that produces them, because a word added to `failure_kind`
and nowhere else would be ranked and rendered as though it had always been
classified. That breadth beats volume. And that `policy` never ranks, because
the whole argument for the owner axis is that robots refusals and bot walls are
counted in full and are not a to-do list.
"""

from __future__ import annotations

import inspect
import re

import pytest

from scrapev3 import faults
from scrapev3.fetch import (ACCESS_VERDICTS, FAILURE_KINDS, NOT_FAILURES,
                            failure_kind, owner_of, severity_of)
from scrapev3.fetch.client import Response


def _resp(**kw) -> Response:
    base = dict(url="https://x.org/a", final_url="https://x.org/a", status=200,
                text="", headers={}, elapsed_s=0.1)
    base.update(kw)
    return Response(**base)


def _row(kind: str, domain: str, n: int = 1) -> faults.FaultRow:
    return faults.FaultRow(run_id="r1", kind=kind, domain=domain, n=n,
                           first_at="2026-08-31 12:00:00",
                           last_at="2026-08-31 12:00:00")


class TestTheConstantMatchesTheFunction:
    """A vocabulary closed by test and not by constant is one import away from
    drifting. These two assertions are what make `FAILURE_KINDS` the truth
    rather than a second opinion."""

    def test_every_word_the_function_returns_is_in_the_constant(self):
        # Reads the source rather than the docs, so a word added to the
        # function and nowhere else fails here rather than being silently
        # ranked with the unknown default.
        returned = set(re.findall(r'return "([a-z0-9_]+)"',
                                  inspect.getsource(failure_kind)))
        assert returned <= set(FAILURE_KINDS), returned - set(FAILURE_KINDS)

    def test_every_word_in_the_constant_is_reachable(self):
        # The other direction: a word left in the constant after the branch
        # that produced it was deleted is a row nobody will ever see.
        returned = set(re.findall(r'return "([a-z0-9_]+)"',
                                  inspect.getsource(failure_kind)))
        assert set(FAILURE_KINDS) <= returned, set(FAILURE_KINDS) - returned

    def test_the_access_ordering_covers_every_verdict(self):
        # `status._worse_access` folds an agency's domains down to one verdict.
        # A verdict missing from its ordering silently sorts as the mildest,
        # so a `refused` host could report as merely `js_rendered`.
        from scrapev3.status import _worse_access

        order = inspect.getsource(_worse_access)
        for verdict in ACCESS_VERDICTS:
            assert f'"{verdict}"' in order, verdict


class TestEveryKindHasASeverityAndAnOwner:

    @pytest.mark.parametrize("kind", [k for k in FAILURE_KINDS
                                      if k not in NOT_FAILURES])
    def test_a_failure_is_classified(self, kind):
        # The totality check. Catches a kind added to the vocabulary without a
        # severity or an owner, which would otherwise take the unknown default
        # and quietly claim to be our most urgent problem.
        assert severity_of(kind) in (1, 2, 3)
        assert owner_of(kind) in ("us", "site", "policy")

    @pytest.mark.parametrize("kind", NOT_FAILURES)
    def test_a_success_scores_zero_not_broken(self, kind):
        # `ok` is known-good, not unknown, so the safe direction is the bottom
        # of the list. Falling through to the unknown default would rank a
        # successful fetch as our most urgent problem.
        assert severity_of(kind) == 0

    def test_an_unknown_kind_is_loud_not_silent(self):
        # The opposite direction from the two above, and deliberately so: a
        # word this map has never seen is a gap in OUR map, so it belongs on
        # our list until someone says otherwise.
        assert severity_of("some-new-fault") == 3
        assert owner_of("some-new-fault") == "us"

    def test_the_access_verdicts_are_owned_too(self):
        # `owner_of` takes either vocabulary, because a caller holding an
        # access verdict has the same question about it.
        assert owner_of("refused") == "policy"
        assert owner_of("unresolved") == "us"


class TestBreadthOutranksVolume:
    """The reason this module exists."""

    def test_twenty_sites_failing_once_outrank_one_site_failing_forty_times(self):
        # `by_failure` sorted on occurrences, so the single broken site won.
        # Twenty publishers demonstrating the same fault is a defect in our
        # code; forty 404s on one host is that host's problem.
        widespread = faults.attention("http_4xx", domains=20, occurrences=20)
        concentrated = faults.attention("http_4xx", domains=1, occurrences=40)
        assert widespread > concentrated

    def test_occurrences_do_not_move_the_score_at_all(self):
        # They are the tiebreak and a displayed column. If they entered the
        # score, the inversion above would come back at some ratio.
        assert (faults.attention("tls", domains=3, occurrences=1)
                == faults.attention("tls", domains=3, occurrences=9_999))

    def test_ours_outranks_theirs_at_equal_breadth(self):
        # A defect we can fix should come before a site oddity that may have no
        # fix at all. dns is ours, tls is theirs, both on five domains.
        assert (faults.attention("dns", domains=5)
                > faults.attention("tls", domains=5))


class TestPolicyIsCountedAndNeverRanked:

    def test_a_robots_refusal_scores_zero_at_any_breadth(self):
        # The README's sentence as arithmetic: "those are sites declining an
        # identified crawler, which is their call." Without the zero weight, 27
        # refusals sit above every defect we could actually fix.
        assert faults.attention("robots", domains=1) == 0
        assert faults.attention("robots", domains=500) == 0
        assert faults.attention("wall", domains=500) == 0

    def test_but_it_is_still_counted_and_attributed(self):
        # Never ranked is not the same as never recorded. "27 targets refused"
        # is worth knowing; it is just not a to-do list.
        rows = [_row("robots", f"r{i}.org", n=3) for i in range(4)]
        assert faults.summarise(rows)["owner"]["policy"] == 12
        assert len(faults.tally(rows)[0].domains) == 4

    def test_a_domain_is_not_worse_for_having_a_robots_file(self):
        # It would otherwise top the "worst domains" table for obeying us.
        rows = [_row("robots", "polite.org"), _row("dns", "broken.org")]
        worst = faults.worst_domains(rows)
        assert [d for d, _, _ in worst] == ["broken.org"]


class TestTheBands:

    def test_a_defect_of_ours_on_two_domains_is_urgent(self):
        # 3 * 2 * 3 = 18. Two unrelated sites hitting the same defect in our
        # code is where it stops being one site's quirk.
        assert faults.band(faults.attention("dns", domains=2)) == "urgent"

    def test_one_domain_is_not_urgent_however_bad(self):
        assert faults.band(faults.attention("dns", domains=1)) != "urgent"

    def test_an_unranked_band_is_quiet_not_loud(self):
        # The asymmetry with `severity_of`, which shouts at an unknown kind. A
        # band that shouted by default would train people to skim the top of
        # the list, which is the one thing the list cannot survive.
        assert faults.band(0.0) == "minor"

    def test_the_thresholds_are_referenced_not_copied(self):
        assert faults.band(faults.URGENT_AT) == "urgent"
        assert faults.band(faults.URGENT_AT - 0.1) != "urgent"
        assert faults.band(faults.NOTABLE_AT) == "notable"


class TestClassifyingARealResponse:

    def test_the_exception_message_never_reaches_the_key(self):
        # The bug this change exists to fix: `by_failure` was keyed on
        # `resp.error`, which is f"{ClassName}: {message}" with the URL inside
        # it, so two timeouts on different URLs were two rows.
        a = _resp(status=0, error="ConnectTimeout: failed to connect to a.org:443")
        b = _resp(status=0, error="ConnectTimeout: failed to connect to b.org:443")
        assert failure_kind(a) == failure_kind(b) == "connect"

    def test_a_wall_answers_200_so_it_is_checked_before_the_status(self):
        # Otherwise a page we never actually got is counted as a success.
        assert failure_kind(_resp(status=200, wall="just a moment")) == "wall"


class TestWhatTheCrawlHandsOver:
    """`CrawlStats` grew the two things it needed to outlive the process."""

    def test_the_histogram_does_not_fragment(self):
        # The regression this whole change exists to prevent. Keyed on
        # `resp.error`, these two were separate rows and "Why fetches failed"
        # filled up with per-article noise.
        from scrapev3.crawl import CrawlStats

        stats = CrawlStats()
        for host in ("a.org", "b.org"):
            resp = _resp(status=0,
                         error=f"ConnectTimeout: failed to connect to {host}:443")
            stats.bump(stats.by_failure, failure_kind(resp))

        assert stats.by_failure == {"connect": 2}

    def test_occurrences_aggregate_and_the_first_supplies_the_sample(self):
        from scrapev3.crawl import CrawlStats

        stats = CrawlStats()
        stats.record_fault("dns", "x.mil", a_id=7, url="https://x.mil/one",
                           detail="DNSError: first")
        stats.record_fault("dns", "x.mil", url="https://x.mil/two",
                           detail="DNSError: second")

        assert stats.faults[("dns", "x.mil")] == [2, 7, "https://x.mil/one",
                                                  "DNSError: first"]

    def test_the_counters_serialise(self):
        # They never did. That is why a run's diagnosis died with the process:
        # `failure_domains` holds sets, which json refuses.
        import json

        from scrapev3.crawl import CrawlStats

        stats = CrawlStats()
        stats.failure_domains.setdefault("dns", set()).add("x.mil")
        stats.record_fault("dns", "x.mil")

        payload = json.loads(json.dumps(stats.to_dict()))
        assert payload["failure_domains"] == {"dns": ["x.mil"]}
        assert "faults" not in payload, "those are rows, not a JSON blob"


class TestTheRollUp:

    def test_one_kind_across_many_domains_is_one_ranked_row(self):
        rows = [_row("dns", f"s{i}.mil", n=2) for i in range(20)]
        ranked = faults.tally(rows)
        assert len(ranked) == 1
        assert len(ranked[0].domains) == 20
        assert ranked[0].occurrences == 40

    def test_ranked_worst_first(self):
        rows = ([_row("dns", f"s{i}.mil") for i in range(20)]
                + [_row("http_4xx", "one.edu", n=40)]
                + [_row("robots", f"r{i}.org") for i in range(30)])
        order = [t.kind for t in faults.tally(rows)]
        assert order[0] == "dns", "widespread and ours comes first"
        assert order[-1] == "robots", "obeying robots.txt is never the top item"

    def test_the_sample_survives_the_summarising(self):
        # "dns x20" with no example is a summary nobody can act on.
        rows = [faults.FaultRow(run_id="r1", kind="dns", domain="x.mil", n=1,
                                first_at="", last_at="",
                                sample_url="https://x.mil/a",
                                sample_detail="DNSError: getaddrinfo failed")]
        assert "getaddrinfo" in faults.tally(rows)[0].sample_detail
