"""What is going wrong with the crawl, for a Python site to render.

The same interface as `faults.php`, and the same rules as `status.py`: every
function returns plain data, columns are selected by name, nothing is rendered.

    import faults
    conn  = faults.connect(host, user, password)
    worst = faults.faults(conn)            # ranked, worst first
    mine  = faults.faults(conn, "us")      # the to-do list

`agency_status` answers "is this publisher being collected?" - one row per
agency, for a grid a publisher might see. This answers "what is wrong with the
crawler?" - one row per kind, across the whole corpus, for whoever operates it.
Do not put it on a publisher-facing page: `dns x20` is our operational detail
and means nothing to a newsroom.

The table is a snapshot of the last pass, rewritten each time and pruned of
anything that stopped happening, so a kind fixed last week disappears rather
than lingering. History is kept on the crawler (`scrapev3 faults --runs 7`).

Copy this file into the site. It imports nothing from scrapev3.
"""

from __future__ import annotations

from typing import Any

import pymysql

# The three bands, worst first. Closed, like `severity` in status.py.
OWNERS = ("us", "site", "policy")

# Selected by name, never `*`, so a column added upstream cannot shift a value.
COLUMNS = ("kind", "severity", "owner", "domains", "occurrences", "score",
           "band", "example_domain", "sample_url", "sample_detail", "run_id",
           "updated_at")
_COLUMN_LIST = ", ".join(COLUMNS)

# `kind` breaks the tie so paging cannot repeat or skip a row - the same
# total-order rule the status grid needs.
_ORDER = "ORDER BY score DESC, occurrences DESC, kind"


def connect(host: str, user: str, password: str, *, port: int = 3306,
            database: str = "scrapev3") -> Any:
    """Connect to the shared state database. Read-only work, so autocommit."""
    return pymysql.connect(
        host=host, port=port, user=user, password=password, database=database,
        charset="utf8mb4", autocommit=True, cursorclass=pymysql.cursors.DictCursor,
    )


def faults(conn: Any, owner: str | None = None,
           limit: int | None = None) -> list[dict]:
    """Every fault kind from the last pass, worst first.

    Ranked by the crawler, not here: `score` is severity x how many domains
    raised it x whose problem it is, and re-deriving that would be a second
    definition of "worth fixing" that drifts from the crawler's. Order by
    `score` and display `band`.

    `policy` rows - a robots.txt we obeyed, a bot wall - score 0 by
    construction and sort to the bottom. They are returned so they can be
    counted, and they are never the top of the list.
    """
    sql, params = f"SELECT {_COLUMN_LIST} FROM crawl_fault", []
    if owner is not None:
        if owner not in OWNERS:
            raise ValueError(f"not an owner: {owner!r}")
        sql += " WHERE owner = %s"
        params.append(owner)
    sql += f" {_ORDER}"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(max(1, int(limit)))

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [_cast(r) for r in cur.fetchall()]


def fault(conn: Any, kind: str) -> dict | None:
    """One kind, or None if it did not occur on the last pass.

    None is a real answer and a good one: the kind is not currently happening.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMN_LIST} FROM crawl_fault WHERE kind = %s",
                    (kind,))
        row = cur.fetchone()
    return _cast(row) if row else None


def summary(conn: Any) -> dict:
    """Counts per owner, plus how fresh the tracker is.

    Show `updated_at`. This table is written by a batch job that can simply
    stop running, and a tracker frozen at last Tuesday looks exactly like a
    quiet week - the same trap as the status grid.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT owner, COUNT(*) AS kinds, SUM(occurrences) AS n, "
                    "MAX(updated_at) AS updated_at, MAX(run_id) AS run_id "
                    "FROM crawl_fault GROUP BY owner")
        rows = cur.fetchall()

    out: dict[str, Any] = {"total": 0, "kinds": 0,
                           "owner": {o: 0 for o in OWNERS},
                           "updated_at": None, "run_id": None}
    for row in rows:
        out["total"] += int(row["n"] or 0)
        out["kinds"] += int(row["kinds"])
        out["owner"][row["owner"]] = int(row["n"] or 0)
        stamp = row["updated_at"]
        if stamp is not None and hasattr(stamp, "isoformat"):
            stamp = stamp.isoformat(sep=" ", timespec="seconds")
        if stamp and (out["updated_at"] is None or stamp > out["updated_at"]):
            out["updated_at"] = stamp
        if row["run_id"] and (out["run_id"] is None
                              or row["run_id"] > out["run_id"]):
            out["run_id"] = row["run_id"]
    return out


def grid(conn: Any, owner: str | None = None, limit: int | None = None) -> dict:
    """Ranked list plus the counts above it, in one call.

    The shape `scrapev3 faults --json` writes, so a page built against a
    fixture keeps working when it is pointed at the database.
    """
    from datetime import datetime, timezone

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary(conn),
        "faults": faults(conn, owner, limit),
    }


def _cast(row: dict) -> dict:
    """Ints as ints, floats as floats, datetimes as ISO strings."""
    row = dict(row)
    for key in ("severity", "domains", "occurrences"):
        if row.get(key) is not None:
            row[key] = int(row[key])
    if row.get("score") is not None:
        row["score"] = float(row["score"])
    stamp = row.get("updated_at")
    if stamp is not None and hasattr(stamp, "isoformat"):
        row["updated_at"] = stamp.isoformat(sep=" ", timespec="seconds")
    return row


if __name__ == "__main__":
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", choices=OWNERS)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--host", default=os.environ.get("SCRAPEV3_DB_HOST", "localhost"))
    ap.add_argument("--user", default=os.environ.get("SCRAPEV3_DB_USER", "website"))
    args = ap.parse_args()

    password = os.environ.get("SCRAPEV3_DB_PASSWORD")
    if not password:
        sys.exit("Set SCRAPEV3_DB_PASSWORD")

    conn = connect(args.host, args.user, password)
    try:
        head = summary(conn)
        print(f"{head['kinds']} kinds, {head['total']} occurrences · "
              f"us {head['owner']['us']} · site {head['owner']['site']} · "
              f"policy {head['owner']['policy']} · updated {head['updated_at']}")
        for f in faults(conn, args.owner, args.limit):
            print(f"  {f['kind']:<26} {f['owner']:<7} {f['band']:<8} "
                  f"{f['domains']:>4} domains  {f['sample_detail'] or ''}"[:110])
    finally:
        conn.close()
