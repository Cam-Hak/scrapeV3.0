"""Read crawl health from the scrapev3 status table, from another codebase.

The read side of the integration; `remove_agency.py` is the write side. Copy
this file into the site. It imports nothing from scrapev3 - no shared
virtualenv, no file paths, no dependency on the crawler being installed. The
only coupling is the table.

    pip install PyMySQL

Data only: every function returns plain dicts and lists. Nothing here formats,
prints, or renders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pymysql

# Worst first. `severity` is a closed three-value vocabulary and the only field
# worth branching on; `health` is open - the crawler may learn new words for new
# faults - so treat it as a label to display, not a condition to test.
SEVERITIES = ("error", "warn", "ok")

# Selected by name, never `*`, so a column added upstream cannot shift a value.
COLUMNS = (
    "a_id", "domain", "newsroom_url", "enabled", "health", "severity", "reason",
    "discovery_method", "targets", "consec_failures", "needs_browser",
    "articles", "articles_recent", "median_body_len", "last_success_at",
    "last_article_at",
    # The inventory half: not the verdict on the agency, but the plan for it -
    # whether discovery is solved, when we last actually pulled a document, and
    # when the schedule comes back.
    "targets_cached", "feed_url", "feed_absent", "probed_at", "conditional_get",
    "next_due_at", "crawl_delay_s", "revisit_period_s", "first_stored_at",
    "last_stored_at", "tns_loaded", "tns_pending",
    "updated_at",
)
_COLUMN_LIST = ", ".join(COLUMNS)

# Worst first, and the default when nobody asks for anything else. FIELD()
# rather than a plain sort on `severity`, because alphabetical reads
# "error, ok, warn" - the one order in which the broken sites are not first.
_RANK = "FIELD(severity, 'error', 'warn', 'ok')"
_ORDER = f"ORDER BY {_RANK}, articles DESC, a_id"

# Any column may be sorted on, and only a column may be: the name is checked
# against this set and never interpolated from what a caller sent. A query
# string is the usual source of a sort key, so this is the one place user input
# gets near the SQL.
SORTABLE = frozenset(COLUMNS)


def order_by(sort: str | None = None, desc: bool = False) -> str:
    """The ORDER BY clause for a sort key, or the default worst-first one.

    Three things it guarantees, all of which matter on real data:

    * **Nulls last in both directions.** MySQL puts them first ascending, and
      "never pulled a document" is not the smallest date - it is the absence of
      one, and it belongs at the bottom either way.
    * **A total order.** `a_id` breaks every tie, always ascending. Sorting by
      `health` leaves two thousand rows tied, and without a tiebreak the same
      page-2 query can return rows that were already on page 1.
    * **`severity` sorts by rank, not alphabetically**, for the reason above.
    """
    if sort is None:
        return _ORDER
    if sort not in SORTABLE:
        raise ValueError(f"not a sortable column: {sort!r}")
    key = _RANK if sort == "severity" else sort
    direction = "DESC" if desc else "ASC"
    return f"ORDER BY ({sort} IS NULL), {key} {direction}, a_id"


def _sortable(value: Any) -> Any:
    """Compare text the way the database does, not the way Python does.

    `agency_status` is utf8mb4_0900_ai_ci, so MySQL treats "A" and "a" as the
    same letter. Python does not - `"Z" < "a"` is true on codepoints - so the
    two orderings diverge the moment a value carries a capital. 250 of the
    newsroom URLs do (`navy.mil/Press-Office`, `centcom.mil/MEDIA`), and the
    live rows and the fixture rows would come back in different orders on the
    same page, intermittently, depending on which hosts happened to collide.

    Case is folded here; accents are not. `ai` also equates "e" and "é", which
    would need full Unicode collation to reproduce - out of proportion for
    columns holding hostnames, URLs and English sentences.
    """
    return value.lower() if isinstance(value, str) else value


def sort_rows(rows: list[dict], sort: str | None = None,
              desc: bool = False) -> list[dict]:
    """The same order as `order_by`, applied in Python.

    For the no-database path: rows read from a JSON fixture have not been
    through MySQL and still have to come out in the order the site would get
    live. `tests/test_client_contract.py` runs both against the same rows and
    requires them to agree - two orderings that differ only sometimes is worse
    than either one.
    """
    if sort is not None and sort not in SORTABLE:
        raise ValueError(f"not a sortable column: {sort!r}")

    if sort is None:
        rank = {s: i for i, s in enumerate(SEVERITIES)}
        return sorted(rows, key=lambda r: (rank.get(r.get("severity"), 9),
                                           -(r.get("articles") or 0),
                                           r.get("a_id")))

    present = [r for r in rows if r.get(sort) is not None]
    absent = [r for r in rows if r.get(sort) is None]

    if sort == "severity":
        rank = {s: i for i, s in enumerate(SEVERITIES)}
        key = lambda r: rank.get(r.get("severity"), 9)          # noqa: E731
    else:
        key = lambda r: _sortable(r.get(sort))                  # noqa: E731

    # Two stable passes rather than one compound key: `reverse=True` would flip
    # the tiebreak as well, and `a_id` ascending is what the SQL does in both
    # directions.
    present.sort(key=lambda r: r.get("a_id"))
    present.sort(key=key, reverse=desc)
    absent.sort(key=lambda r: r.get("a_id"))
    return present + absent


def connect(host: str, user: str, password: str, *, port: int = 3306,
            database: str = "scrapev3") -> Any:
    """Connect to the shared state database.

    Read-only work, so autocommit is fine and there is no transaction to leak.
    """
    return pymysql.connect(
        host=host, port=port, user=user, password=password, database=database,
        charset="utf8mb4", autocommit=True, cursorclass=pymysql.cursors.DictCursor,
    )


def statuses(conn: Any, *, severity: str | None = None, health: str | None = None,
             domain: str | None = None, search: str | None = None,
             uncached: bool = False, due: bool = False,
             sort: str | None = None, desc: bool = False,
             limit: int | None = None) -> list[dict]:
    """Every agency's status, worst first unless `sort` says otherwise.

    `sort` is a column name - see `SORTABLE`. Anything else raises rather than
    reaching the SQL.
    """
    where, params = [], []
    for column, value in (("severity", severity), ("health", health),
                          ("domain", domain)):
        if value:
            where.append(f"{column} = %s")
            params.append(value)
    # Not a health word, and deliberately not derived from one: an agency can be
    # perfectly healthy and still own a newsroom the cascade has never solved.
    if uncached:
        where.append("targets_cached < targets")
    if due:
        where.append("enabled = 1 AND (next_due_at IS NULL "
                     "OR next_due_at <= UTC_TIMESTAMP())")
    if search:
        where.append("(domain LIKE %s OR newsroom_url LIKE %s)")
        params += [f"%{search}%", f"%{search}%"]

    sql = (f"SELECT {_COLUMN_LIST} FROM agency_status"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " " + order_by(sort, desc))
    if limit is not None:
        sql += " LIMIT %s"
        params.append(max(1, int(limit)))

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [_cast(r) for r in cur.fetchall()]


def status(conn: Any, a_id: int) -> dict | None:
    """One agency, or None if the crawler does not hold it.

    None is a real answer: the agency is not in the frontier at all - never
    seeded, or removed on request. Distinct from an agency with nothing to
    report, which returns a row that says so.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMN_LIST} FROM agency_status WHERE a_id = %s",
                    (a_id,))
        row = cur.fetchone()
    return _cast(row) if row else None


def statuses_for(conn: Any, a_ids: list[int]) -> dict[int, dict]:
    """Several agencies at once, keyed by a_id. One query, not one per row."""
    ids = sorted({int(a) for a in a_ids})
    if not ids:
        return {}
    marks = ", ".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMN_LIST} FROM agency_status "
                    f"WHERE a_id IN ({marks})", tuple(ids))
        return {int(r["a_id"]): _cast(r) for r in cur.fetchall()}


def summary(conn: Any) -> dict:
    """Counts per health word and per severity, plus how fresh the grid is.

    Show `updated_at`. The table is refreshed by a batch job that can simply
    stop running, and a frozen grid looks exactly like a healthy one.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT health, severity, COUNT(*) AS n, "
                    "MAX(updated_at) AS updated_at "
                    "FROM agency_status GROUP BY health, severity")
        rows = cur.fetchall()

    out: dict[str, Any] = {"total": 0, "health": {},
                           "severity": {s: 0 for s in SEVERITIES},
                           "updated_at": None}
    for row in rows:
        n = int(row["n"])
        out["total"] += n
        out["health"][row["health"]] = out["health"].get(row["health"], 0) + n
        out["severity"][row["severity"]] = out["severity"].get(row["severity"], 0) + n
        if out["updated_at"] is None or row["updated_at"] > out["updated_at"]:
            out["updated_at"] = row["updated_at"]
    out["health"] = dict(sorted(out["health"].items(), key=lambda kv: -kv[1]))
    return out


def grid(conn: Any, **filters: Any) -> dict:
    """Rows plus the counts above them, in the same shape as
    `scrapev3 status --json`.

    `generated_at` is when this was read, which is not `summary["updated_at"]` -
    that is when the crawler last wrote a row. A page needs both: one says how
    fresh the query is, the other says how fresh the data is, and a batch job
    that stopped running only moves the first.
    """
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary(conn),
        "agencies": statuses(conn, **filters),
    }


def _cast(row: dict) -> dict:
    """Booleans as booleans, datetimes as ISO strings.

    MySQL's TINYINT(1) arrives as 0/1, which is truthy in the wrong direction
    often enough to be worth fixing once here rather than at each call site.
    """
    row = dict(row)
    for key in ("enabled", "needs_browser", "feed_absent", "conditional_get"):
        if key in row:
            row[key] = bool(row[key])
    if row.get("crawl_delay_s") is not None:
        row["crawl_delay_s"] = float(row["crawl_delay_s"])
    for key in ("last_success_at", "last_article_at", "probed_at", "next_due_at",
                "first_stored_at", "last_stored_at", "updated_at"):
        value = row.get(key)
        if value is not None and hasattr(value, "isoformat"):
            row[key] = value.isoformat(sep=" ", timespec="seconds")
    return row


if __name__ == "__main__":
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--a-id", type=int, default=None)
    ap.add_argument("--severity", choices=list(SEVERITIES), default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--host", default=os.environ.get("SCRAPEV3_DB_HOST", "localhost"))
    ap.add_argument("--user", default=os.environ.get("SCRAPEV3_DB_USER", "website"))
    args = ap.parse_args()

    password = os.environ.get("SCRAPEV3_DB_PASSWORD")
    if not password:
        sys.exit("Set SCRAPEV3_DB_PASSWORD")

    conn = connect(args.host, args.user, password)
    try:
        if args.a_id is not None:
            print(json.dumps(status(conn, args.a_id), indent=2, default=str))
        else:
            print(json.dumps(grid(conn, severity=args.severity, limit=args.limit),
                             indent=2, default=str))
    finally:
        conn.close()
