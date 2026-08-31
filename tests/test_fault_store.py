"""Keeping a run's failures past the end of the run.

`CrawlStats` was built, printed as eight strings truncated to 110 characters,
and thrown away. So "is this new?", "is it spreading?" and "which of these is
mine?" could only be answered by running the crawl again and watching it.

What is worth pinning is mostly about what the store deliberately does NOT do:
it does not keep a row per occurrence, it does not key on `a_id`, and it does
not store severity or owner. The last one is the load-bearing choice - derived
at read time means a rule change re-ranks history, and a stored row can never
disagree with the classifier that would judge it today.
"""

from __future__ import annotations

import json

import pytest

from scrapev3 import faults
from scrapev3.faults import FaultStore


@pytest.fixture()
def store(tmp_path):
    s = FaultStore(tmp_path)
    yield s
    s.close()


class TestOneRowPerKindAndDomain:

    def test_forty_occurrences_are_one_row_with_a_count(self, store):
        # A pass over 2,400 targets produces thousands of occurrences whose only
        # use is being counted. The questions the store answers are "how many"
        # and "which sites"; a count per domain answers both.
        store.start_run("r1", command="crawl")
        for _ in range(40):
            store.record("r1", "http_4xx", "hccs.edu", url="https://hccs.edu/a",
                         detail="HTTP 404")

        rows = store.rows(run_id="r1")
        assert len(rows) == 1
        assert rows[0].n == 40

    def test_the_first_occurrence_supplies_the_sample(self, store):
        # One example is the difference between "40 x http_4xx on hccs.edu" and
        # being able to go and look at one.
        store.start_run("r1", command="crawl")
        store.record("r1", "dns", "x.mil", url="https://x.mil/first",
                     detail="DNSError: getaddrinfo failed")
        store.record("r1", "dns", "x.mil", url="https://x.mil/second",
                     detail="DNSError: something else")

        row = store.rows(run_id="r1")[0]
        assert row.sample_url == "https://x.mil/first"
        assert "getaddrinfo" in row.sample_detail

    def test_house_gov_is_one_row_not_four_hundred(self, store):
        # `house.gov` carries 417 agencies, and a fetch fault is a property of
        # the domain - the pacing and blame unit - not of whichever legislator
        # page happened to hit it. In the key it would explode into 417 rows
        # all saying the same thing.
        store.start_run("r1", command="crawl")
        for a_id in range(400):
            store.record("r1", "timeout", "house.gov", a_id=a_id)

        rows = store.rows(run_id="r1")
        assert len(rows) == 1
        assert rows[0].n == 400
        assert rows[0].a_id == 0, "the first a_id seen, kept as a join handle"

    def test_different_domains_stay_apart(self, store):
        # The attribution half. "A reason without attribution is only half a
        # diagnosis."
        store.start_run("r1", command="crawl")
        for i in range(20):
            store.record("r1", "dns", f"s{i}.mil")

        assert len(store.rows(run_id="r1")) == 20


class TestSeverityAndOwnerAreNotColumns:

    def test_reclassifying_changes_history(self, store, monkeypatch):
        # The property that justifies deriving them. `audit --rescore` can
        # re-judge saved evidence without re-fetching; this is the same choice,
        # and it is why a row written last month cannot disagree with today's
        # rules.
        store.start_run("r1", command="crawl")
        store.record("r1", "tls", "a.org")
        store.record("r1", "tls", "b.org")
        before = faults.tally(store.rows(run_id="r1"))[0].score

        monkeypatch.setitem(faults.__dict__["_OWNER_WEIGHT"], "site", 9.0)
        after = faults.tally(store.rows(run_id="r1"))[0].score

        assert after > before, "a stored run re-ranks when the rule changes"

    def test_the_schema_carries_no_verdict(self, store):
        # If severity or owner were columns, the row above could disagree with
        # the classifier and nothing would raise.
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(fault)")}
        assert "severity" not in cols
        assert "owner" not in cols
        assert "score" not in cols


class TestRunsAndPruning:

    def test_the_newest_runs_survive_and_the_rest_go(self, store):
        # Thirty runs is a month at daily cadence, which is the window in which
        # "is this new?" is still answerable.
        for i in range(6):
            rid = f"2026083{i}-120000"
            store.start_run(rid, command="crawl")
            store.record(rid, "dns", "x.mil")

        store.prune(keep=2)
        left = store.run_ids(limit=99)

        assert left == ["20260835-120000", "20260834-120000"]
        assert {r.run_id for r in store.rows(runs=99)} == set(left)

    def test_pruning_an_empty_store_is_harmless(self, store):
        assert store.prune(keep=5) == 0

    def test_a_run_records_its_scope_and_its_counters(self, store):
        # `stats_json` is the serialisation CrawlStats never had.
        store.start_run("r1", command="crawl", scope="--domain x.org")
        store.finish_run("r1", domains=3, targets=9, stats={"failed": 2})

        run = store.run("r1")
        assert run["scope"] == "--domain x.org"
        assert run["domains"] == 3 and run["targets"] == 9
        raw = store.db.execute(
            "SELECT stats_json FROM fault_run WHERE run_id='r1'").fetchone()[0]
        assert json.loads(raw)["failed"] == 2

    def test_several_runs_can_be_read_together(self, store):
        # "Is this new?" needs more than one run in the answer.
        for rid in ("20260830-120000", "20260831-120000"):
            store.start_run(rid, command="crawl")
            store.record(rid, "dns", "x.mil")

        assert len(store.rows(runs=1)) == 1
        assert len(store.rows(runs=2)) == 2


class TestFiltering:

    @pytest.fixture()
    def filled(self, store):
        store.start_run("r1", command="crawl")
        store.record("r1", "dns", "a.mil")
        store.record("r1", "tls", "b.org")
        store.record("r1", "robots", "c.org")
        return store

    def test_by_owner(self, filled):
        # The to-do list. `owner` is filtered in Python because it is derived -
        # filtering it in SQL would mean storing it.
        assert [r.kind for r in filled.rows(owner="us")] == ["dns"]
        assert [r.kind for r in filled.rows(owner="policy")] == ["robots"]

    def test_by_kind_and_by_domain(self, filled):
        assert [r.domain for r in filled.rows(kind="tls")] == ["b.org"]
        assert [r.kind for r in filled.rows(domain="c.org")] == ["robots"]

    def test_an_empty_store_answers_with_nothing_rather_than_raising(self, store):
        assert store.rows() == []
        assert store.run_ids() == []


class TestThePayload:

    def test_json_carries_the_derived_fields_for_a_reader_with_no_classifier(self, store):
        store.start_run("r1", command="crawl")
        store.record("r1", "dns", "x.mil", n=4)

        payload = json.loads(faults.to_json(store.rows(run_id="r1")))
        assert payload["faults"][0]["owner"] == "us"
        assert payload["faults"][0]["severity"] == 3
        assert payload["ranked"][0]["kind"] == "dns"
        assert payload["summary"]["occurrences"] == 4
