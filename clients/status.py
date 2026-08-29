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
    "last_article_at", "updated_at",
)
_COLUMN_LIST = ", ".join(COLUMNS)
_ORDER = "ORDER BY FIELD(severity, 'error', 'warn', 'ok'), articles DESC, a_id"


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
             limit: int | None = None) -> list[dict]:
    """Every agency's status, worst first."""
    where, params = [], []
    for column, value in (("severity", severity), ("health", health),
                          ("domain", domain)):
        if value:
            where.append(f"{column} = %s")
            params.append(value)
    if search:
        where.append("(domain LIKE %s OR newsroom_url LIKE %s)")
        params += [f"%{search}%", f"%{search}%"]

    sql = (f"SELECT {_COLUMN_LIST} FROM agency_status"
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" {_ORDER}")
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
    `scrapev3 status --json`."""
    return {"summary": summary(conn), "agencies": statuses(conn, **filters)}


def _cast(row: dict) -> dict:
    """Booleans as booleans, datetimes as ISO strings.

    MySQL's TINYINT(1) arrives as 0/1, which is truthy in the wrong direction
    often enough to be worth fixing once here rather than at each call site.
    """
    row = dict(row)
    for key in ("enabled", "needs_browser"):
        if key in row:
            row[key] = bool(row[key])
    for key in ("last_success_at", "last_article_at", "updated_at"):
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
