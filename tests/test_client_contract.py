"""The data layer the website actually calls.

`clients/status.py` is not imported by the crawler - it is a file the other
codebase copies - so nothing else in this suite touches it, and it was the one
part of the contract with no test at all.

Sorting is the substance here. It exists twice by necessity: once as SQL for a
site with a database, once in Python for a site reading the JSON fixture. Two
orderings that agree only sometimes are worse than either alone, so the rules
below pin the three properties that make them agree - nulls last, a total
order, and text compared the way the database compares it.

The SQL half is verified against a live MySQL by hand (`order_by` produces the
clause, and 2,399 real rows come back in the same order `sort_rows` puts them
in). What is pinned offline is everything that does not need a server.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "clients" / "status.py"


def _load():
    """Import the client by path. It is not on sys.path and must not need to be."""
    spec = importlib.util.spec_from_file_location("scrapev3_client_status", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _load()


def _rows(*values, key="last_stored_at"):
    """Rows carrying one column, with a_id in the order given."""
    return [{"a_id": i, key: v, "severity": "warn", "articles": 0}
            for i, v in enumerate(values, start=1)]


class TestNothingButAColumnNameReachesTheSql:
    """A sort key normally arrives from a query string."""

    def test_every_published_column_can_be_sorted_on(self):
        # The website selects all of them; a column it can show and not order
        # by is an arbitrary gap for it to work around.
        assert client.SORTABLE == frozenset(client.COLUMNS)

    @pytest.mark.parametrize("attack", [
        "a_id; DROP TABLE agency_status",
        "a_id, (SELECT 1)",
        "articles UNION SELECT 1",
        "", "*", "1",
    ])
    def test_anything_else_raises_instead_of_being_interpolated(self, attack):
        with pytest.raises(ValueError):
            client.order_by(attack)
        with pytest.raises(ValueError):
            client.sort_rows([], attack)

    def test_the_clause_only_ever_names_a_whitelisted_column(self):
        for column in client.COLUMNS:
            assert column in client.order_by(column)


class TestTheThreePropertiesThatMakeBothPathsAgree:

    def test_nulls_are_last_in_both_directions(self):
        # MySQL puts them first ascending. "Never pulled a document" is not the
        # smallest date - it is the absence of one, and a grid sorted by
        # "oldest first" that opens on a screen of blanks is useless.
        rows = _rows("2026-01-01 00:00:00", None, "2025-01-01 00:00:00", None)
        for desc in (False, True):
            order = [r["a_id"] for r in client.sort_rows(rows, "last_stored_at", desc)]
            assert order[-2:] == [2, 4], f"nulls not last with desc={desc}"

    def test_ties_break_on_a_id_ascending_in_both_directions(self):
        # Sorting by `health` leaves two thousand rows tied. Without a total
        # order the same page-2 query returns rows that were already on page 1,
        # and the tiebreak must not flip when the primary key does.
        rows = _rows("same", "same", "same")
        assert [r["a_id"] for r in client.sort_rows(rows, "last_stored_at")] == [1, 2, 3]
        assert [r["a_id"] for r in client.sort_rows(rows, "last_stored_at", True)] == [1, 2, 3]

    def test_text_is_compared_the_way_the_database_compares_it(self):
        # agency_status is utf8mb4_0900_ai_ci, so MySQL sorts "a" before "Z".
        # Python's `<` sorts on codepoints and puts "Z" first. 250 of the live
        # newsroom URLs carry a capital, so this diverged on real data.
        rows = _rows("https://x.mil/ZULU", "https://x.mil/alpha",
                     key="newsroom_url")
        assert [r["a_id"] for r in client.sort_rows(rows, "newsroom_url")] == [2, 1]

    def test_case_only_differences_are_a_tie_not_an_order(self):
        # `ai_ci` calls them equal, so a_id decides - not the capital.
        rows = _rows("https://x.org/AAA", "https://x.org/aaa", key="newsroom_url")
        assert [r["a_id"] for r in client.sort_rows(rows, "newsroom_url")] == [1, 2]


class TestSeverityIsRankedNotSpelled:

    def test_sorting_by_severity_puts_the_broken_sites_first(self):
        # Alphabetical reads "error, ok, warn" - the one order in which the
        # sites that need attention are not at the top.
        rows = [{"a_id": 1, "severity": "ok", "articles": 0},
                {"a_id": 2, "severity": "warn", "articles": 0},
                {"a_id": 3, "severity": "error", "articles": 0}]
        assert [r["a_id"] for r in client.sort_rows(rows, "severity")] == [3, 2, 1]

    def test_the_sql_ranks_it_too(self):
        assert "FIELD(severity" in client.order_by("severity")

    def test_an_unknown_band_sorts_last_rather_than_crashing(self):
        # `severity_of` resolves an unknown health word to warn, but a row could
        # still arrive from a newer crawler than this client.
        rows = [{"a_id": 1, "severity": "moon", "articles": 0},
                {"a_id": 2, "severity": "error", "articles": 0}]
        assert [r["a_id"] for r in client.sort_rows(rows, "severity")] == [2, 1]


class TestTheDefaultOrder:

    def test_worst_first_then_busiest_then_a_id(self):
        rows = [{"a_id": 1, "severity": "ok", "articles": 5},
                {"a_id": 2, "severity": "error", "articles": 0},
                {"a_id": 3, "severity": "ok", "articles": 9},
                {"a_id": 4, "severity": "warn", "articles": 0}]
        assert [r["a_id"] for r in client.sort_rows(rows)] == [2, 4, 3, 1]

    def test_it_is_what_you_get_without_a_sort_key(self):
        assert client.order_by() == client.order_by(None)
        assert "FIELD(severity" in client.order_by()

    def test_no_sort_key_does_not_mean_insertion_order(self):
        rows = [{"a_id": 9, "severity": "ok", "articles": 0},
                {"a_id": 1, "severity": "error", "articles": 0}]
        assert [r["a_id"] for r in client.sort_rows(rows)] == [1, 9]


class TestMixedTypesDoNotCrashTheComparator:
    """Columns are uniform after `_cast`, but a fixture is a file anyone can edit."""

    def test_a_column_of_numbers_sorts_numerically_not_as_text(self):
        # "10" < "9" as text. Articles counts cross that boundary constantly.
        rows = _rows(9, 10, 100, key="articles")
        assert [r["a_id"] for r in client.sort_rows(rows, "articles")] == [1, 2, 3]

    def test_booleans_sort_without_special_casing(self):
        rows = _rows(True, False, True, key="needs_browser")
        assert [r["a_id"] for r in client.sort_rows(rows, "needs_browser")] == [2, 1, 3]

    def test_a_missing_key_is_treated_as_null_not_an_error(self):
        # An older fixture, written before a column existed.
        rows = [{"a_id": 1, "severity": "ok", "articles": 0},
                {"a_id": 2, "severity": "ok", "articles": 0,
                 "last_stored_at": "2026-01-01 00:00:00"}]
        assert [r["a_id"] for r in client.sort_rows(rows, "last_stored_at")] == [2, 1]


class TestTheQueryTheSiteActuallySends:
    """A fake connection, so the SQL can be read without a server."""

    class _Cursor:
        def __init__(self, seen): self.seen = seen
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=()): self.seen.append((sql, params))
        def fetchall(self): return []

    class _Conn:
        def __init__(self): self.seen = []
        def cursor(self): return TestTheQueryTheSiteActuallySends._Cursor(self.seen)

    def _sql(self, **kwargs):
        conn = self._Conn()
        client.statuses(conn, **kwargs)
        return conn.seen[0]

    def test_filters_are_parameters_never_string_interpolation(self):
        sql, params = self._sql(domain="x.org", search="'; DROP TABLE t; --")
        assert "DROP TABLE" not in sql
        assert "'; DROP TABLE t; --" in str(params)
        assert sql.count("%s") == len(params)

    def test_a_sort_key_reaches_the_clause_and_the_filters_still_bind(self):
        sql, params = self._sql(severity="error", sort="last_stored_at", desc=True)
        assert "ORDER BY (last_stored_at IS NULL), last_stored_at DESC, a_id" in sql
        assert params == ("error",)

    def test_the_limit_is_a_parameter_too(self):
        sql, params = self._sql(limit=50)
        assert sql.rstrip().endswith("LIMIT %s")
        assert params[-1] == 50

    def test_uncached_compares_two_columns_rather_than_taking_a_value(self):
        # `targets_cached < targets` is the condition; there is nothing for a
        # caller to supply, so there must be no placeholder for it either.
        sql, params = self._sql(uncached=True)
        assert "targets_cached < targets" in sql
        assert params == ()

    def test_every_column_is_selected_by_name(self):
        sql, _ = self._sql()
        assert "SELECT *" not in sql
        for column in client.COLUMNS:
            assert column in sql
