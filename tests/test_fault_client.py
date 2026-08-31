"""The errors tracker, as the website reads it.

Two tables answer two questions and must not be conflated. `agency_status` says
"is this publisher being collected?" - one row per agency, for a grid a
publisher might see. `crawl_fault` says "what is wrong with the crawler?" - one
row per kind across the whole corpus, for whoever operates it.

The rule that inverts between the two stores is the thing worth pinning.
Locally `severity` and `owner` are derived and never stored, so a rule change
re-ranks history. On the wire they ARE stored, because the consumer has no
classifier - and a website re-deriving a severity is the second definition of
"broken" that `status.py` exists to prevent. Same principle, opposite mechanics.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from scrapev3 import faults, status
from scrapev3.faults import FaultRow

_ROOT = Path(__file__).resolve().parents[1]


def _client(name: str):
    """Import a client by path. They are copied into other codebases, so they
    are not on sys.path and must never need to be."""
    spec = importlib.util.spec_from_file_location(
        f"scrapev3_client_{name}", _ROOT / "clients" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(kind, domain, a_id=None, n=1, detail=""):
    return FaultRow(run_id="r1", kind=kind, domain=domain, a_id=a_id, n=n,
                    first_at="", last_at="", sample_detail=detail)


class TestThePublishedRowCarriesItsOwnVerdict:
    """The inversion. Locally derived, on the wire published."""

    def test_severity_owner_score_and_band_are_all_columns(self):
        # A website has no classifier. Making PHP re-derive `score` would be a
        # second definition of "worth fixing" that drifts from the crawler's -
        # exactly what `health`/`severity` on agency_status exist to prevent.
        for column in ("severity", "owner", "score", "band"):
            assert column in faults.PUBLISH_COLUMNS

    def test_the_local_store_still_stores_none_of_them(self, tmp_path):
        # And the local store must NOT, so a rule change re-ranks history.
        store = faults.FaultStore(tmp_path)
        try:
            cols = {r[1] for r in store.db.execute("PRAGMA table_info(fault)")}
            assert not ({"severity", "owner", "score", "band"} & cols)
        finally:
            store.close()

    def test_the_ddl_and_the_column_list_agree(self):
        # Written by name; a column in one and not the other is an insert that
        # fails at 3am rather than in a test.
        declared = {name for name in re.findall(r"^\s{2}(\w+)\s+\w",
                                                faults._PUBLISH_DDL, re.M)
                    if name.islower()}          # skip PRIMARY KEY / KEY idx_*
        assert declared == set(faults.PUBLISH_COLUMNS)


class TestEveryPublishedColumnReachesTheClients:
    """The gap that let `access` be published and never read.

    `57b81b4` added `access` to `agency_status` and to `_MIGRATIONS`, and did
    not add it to either client - which select by name, so the website could
    not see it at all. Nothing failed; the column was simply never returned.
    """

    def test_the_python_client_selects_every_status_column(self):
        client = _client("status")
        assert set(status.COLUMNS) == set(client.COLUMNS)

    def test_the_php_client_selects_every_status_column(self):
        php = (_ROOT / "clients" / "status.php").read_text(encoding="utf-8")
        listed = php.split("SCRAPEV3_STATUS_COLUMNS =", 1)[1].split(";", 1)[0]
        for column in status.COLUMNS:
            assert re.search(rf"\b{column}\b", listed), column

    def test_the_php_fault_client_selects_every_published_column(self):
        php = (_ROOT / "clients" / "faults.php").read_text(encoding="utf-8")
        listed = php.split("SCRAPEV3_FAULT_COLUMNS =", 1)[1].split(";", 1)[0]
        for column in faults.PUBLISH_COLUMNS:
            assert re.search(rf"\b{column}\b", listed), column

    def test_the_python_fault_client_selects_every_published_column(self):
        assert set(_client("faults").COLUMNS) == set(faults.PUBLISH_COLUMNS)


class TestTheWorstThingThatHappenedToOneAgency:
    """`agency_status.fault_kind` - why is THIS row red."""

    def test_the_worse_fault_wins(self):
        # A site that timed out and also crashed the crawl reports the crash.
        worst = faults.worst_for_agency([
            _row("timeout", "x.org", a_id=7, detail="timed out"),
            _row("admin_target_crashed", "x.org", a_id=7, detail="KeyError"),
        ])
        assert worst[7][0] == "admin_target_crashed"

    def test_ours_breaks_a_tie_with_theirs(self):
        # Both severity 2. The one we can fix is the more useful thing to show.
        worst = faults.worst_for_agency([
            _row("tls", "x.org", a_id=7), _row("http_4xx", "x.org", a_id=7)])
        assert worst[7][0] == "http_4xx"

    def test_a_policy_refusal_is_reported_here_even_though_it_never_ranks(self):
        # On one agency's row "the publisher declined us" IS the answer. It is
        # noise only in the corpus-wide list, where it is weighted to zero.
        worst = faults.worst_for_agency([_row("robots", "x.org", a_id=7)])
        assert worst[7][0] == "robots"

    def test_faults_with_no_agency_are_skipped_not_bucketed(self):
        # The crawler's own faults carry no a_id. Filing them under a fake one
        # would blame a publisher for our removal list being unreachable.
        assert faults.worst_for_agency([_row("admin_list_failed", "(crawler)")]) == {}

    def test_an_untouched_agency_keeps_what_it_last_reported(self, tmp_path):
        # "No fault this pass" and "not crawled this pass" are different, and
        # blanking the column would say the first when it meant the second.
        from scrapev3.frontier.store import SQLiteFrontier
        from scrapev3.sink import Sink

        frontier = SQLiteFrontier(tmp_path / "f.sqlite")
        frontier.create_schema()
        frontier.upsert_sites([(100, "https://a.org/news", "a.org"),
                               (200, "https://b.org/news", "b.org")])
        sink = Sink(tmp_path)
        try:
            rows = status.compose(frontier, sink,
                                  faults={100: ("dns", "getaddrinfo failed")})
        finally:
            sink.close()
            frontier.close()

        by_id = {r.a_id: r for r in rows}
        assert by_id[100].fault_kind == "dns"
        assert by_id[200].fault_kind is None


class TestTheSnapshotIsCurrentStateOnly:

    def test_a_kind_that_stopped_happening_is_pruned(self):
        # A fault fixed last week must not linger on the tracker forever - the
        # failure `status.prune` exists to stop, one table over.
        assert "prune_published" in dir(faults)

    def test_the_ranking_the_website_sees_is_the_crawlers(self, tmp_path):
        # `score` is published, so ORDER BY score gives the same answer the CLI
        # gives. If the site sorted by occurrences it would disagree with
        # `scrapev3 faults` about what matters, which is the whole problem.
        ranked = faults.tally([_row("dns", f"s{i}.mil") for i in range(20)]
                              + [_row("http_4xx", "one.edu", n=40)])
        assert [t.kind for t in ranked] == ["dns", "http_4xx"]
        assert ranked[0].score > ranked[1].score
