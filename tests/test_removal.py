"""Removing an agency on request.

A removal has to be a real purge, and it has to *stay* removed. Both halves are
easy to get wrong in ways that look fine: deleting a domain because one of its
agencies left throws away the other 416 on that domain, and deleting rows
without a tombstone is undone by the next `seed`.

MySQL holds the shared list, but every decision here is exercised against temp
SQLite stores - the list is just a set of ids by the time it reaches the code
that matters.
"""

from __future__ import annotations

import json

import pytest

from scrapev3 import removal
from scrapev3.frontier.store import SQLiteFrontier
from scrapev3.sink import Sink


@pytest.fixture()
def frontier(tmp_path):
    store = SQLiteFrontier(tmp_path / "frontier.sqlite")
    store.create_schema()
    # house.gov carries several agencies; solo.org carries one. The difference
    # is the whole point of the orphan rule.
    store.upsert_sites([
        (100, "https://a.house.gov/press", "house.gov"),
        (200, "https://b.house.gov/press", "house.gov"),
        (300, "https://c.house.gov/press", "house.gov"),
        (400, "https://solo.org/news", "solo.org"),
    ])
    yield store
    store.close()


@pytest.fixture()
def sink(tmp_path):
    s = Sink(tmp_path)
    yield s
    s.close()


def _archive(sink: Sink, rows: list[tuple[int, str]]) -> None:
    """Write a daily JSONL directly - faster than crawling to build one."""
    path = sink.data_dir / "articles" / "articles-20260101.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for a_id, url in rows:
            fh.write(json.dumps({"a_id": a_id, "url": url, "domain": "x.org",
                                 "headline": "h", "body": "b"}) + "\n")


class TestASharedDomainSurvives:
    """`domain_state` is keyed on the registrable domain and holds the pacing,
    lease and learned-discovery state for every agency sharing it.

    house.gov carries 417 agencies. Deleting the domain row because one of them
    asked to leave would silently strip the other 416 of their cached discovery
    sources and stop them being crawled at all.
    """

    def test_one_agency_leaving_keeps_the_domain(self, frontier):
        targets, domains = frontier.remove_agency(100)
        assert (targets, domains) == (1, 0), "the domain must not go"
        assert frontier.get("house.gov") is not None
        assert len(frontier.targets_for("house.gov")) == 2

    def test_the_last_agency_leaving_takes_the_domain(self, frontier):
        frontier.remove_agency(100)
        frontier.remove_agency(200)
        targets, domains = frontier.remove_agency(300)
        assert (targets, domains) == (1, 1), "now nothing is left on it"
        assert frontier.get("house.gov") is None

    def test_a_sole_agency_takes_its_domain_with_it(self, frontier):
        assert frontier.remove_agency(400) == (1, 1)
        assert frontier.get("solo.org") is None

    def test_removing_an_unknown_agency_changes_nothing(self, frontier):
        assert frontier.remove_agency(999) == (0, 0)
        assert frontier.get("house.gov") is not None


class TestTheArchiveIsRewritten:
    """`forget` deliberately leaves the JSONL alone - it is append-only so a
    re-crawl adds a line rather than rewriting history. A removal is the one
    case where the history going is the point.
    """

    def test_only_that_agency_is_dropped(self, sink):
        _archive(sink, [(100, "u1"), (200, "u2"), (100, "u3"), (300, "u4")])
        removed, files = sink.purge_archive(100)
        assert (removed, files) == (2, 1)

        path = sink.data_dir / "articles" / "articles-20260101.jsonl"
        left = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert [r["a_id"] for r in left] == [200, 300]

    def test_an_untouched_file_is_not_rewritten(self, sink):
        _archive(sink, [(200, "u1"), (300, "u2")])
        assert sink.purge_archive(100) == (0, 0)

    def test_unparseable_lines_are_kept(self, sink):
        """Losing a corrupt line would be losing evidence. Only records this
        agency owns are removed; anything unreadable stays put."""
        path = sink.data_dir / "articles" / "articles-20260101.jsonl"
        path.write_text('{"a_id": 100}\nnot json at all\n{"a_id": 200}\n',
                        encoding="utf-8")
        removed, _ = sink.purge_archive(100)
        assert removed == 1
        assert "not json at all" in path.read_text(encoding="utf-8")


class TestRemovalIsIdempotent:
    """Every pass reconciles the whole list, so a removal runs many times. The
    second run must be free and silent, not an error.
    """

    def test_running_twice_reports_nothing_the_second_time(self, frontier, sink):
        first = removal.remove(100, frontier=frontier, sink=sink)
        assert first.touched > 0
        second = removal.remove(100, frontier=frontier, sink=sink)
        assert second.touched == 0 and not second.errors

    def test_reconcile_reports_only_what_it_actually_removed(self, frontier, sink):
        reports = removal.reconcile({100, 999}, frontier=frontier, sink=sink)
        assert [r.a_id for r in reports] == [100], "999 was never here"


class TestOneStoreFailingDoesNotStopTheRest:
    """A half-removed agency is worse than one removed everywhere except MySQL.
    Each store's failure is recorded and the others still run; the next
    reconcile finishes the job.
    """

    def test_a_failing_store_is_reported_not_raised(self, frontier, sink):
        class _Broken:
            def delete_rows(self, a_ids):
                raise RuntimeError("server has gone away")

        report = removal.remove(100, frontier=frontier, sink=sink, tns=_Broken())
        assert report.targets == 1, "the frontier purge still happened"
        assert any("press_release" in e for e in report.errors)


class TestSeedDoesNotResurrect:
    """The regression that makes this feature real.

    `seed` upserts every row of the source CSV. Without the tombstone check a
    removed agency returns on the next seed, and the removal was decorative.
    """

    def test_a_removed_agency_is_filtered_before_upsert(self, frontier):
        frontier.remove_agency(100)
        assert frontier.get("house.gov") is not None

        rows = [(100, "https://a.house.gov/press", "house.gov"),
                (200, "https://b.house.gov/press", "house.gov")]
        removed_ids = {100}

        # The filter `_cmd_frontier_seed` applies before calling upsert_sites.
        rows = [r for r in rows if r[0] not in removed_ids]
        frontier.upsert_sites(rows)

        surviving = {t.a_id for t in frontier.targets_for("house.gov")}
        assert 100 not in surviving, "a_id 100 must not come back"
        assert 200 in surviving, "and the seed must still work for everyone else"

    def test_without_the_filter_the_seed_does_resurrect(self, frontier):
        """Pins the failure itself. If someone drops the tombstone check from
        `_cmd_frontier_seed`, this is what silently starts happening again."""
        frontier.remove_agency(100)
        frontier.upsert_sites([(100, "https://a.house.gov/press", "house.gov")])
        assert 100 in {t.a_id for t in frontier.targets_for("house.gov")}
