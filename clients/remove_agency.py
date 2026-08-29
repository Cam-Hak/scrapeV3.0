"""Remove an agency from the scrapev3 crawl, from another Python codebase.

Copy this file into the site. It deliberately imports nothing from scrapev3 -
no shared virtualenv, no file paths, no dependency on the crawler being
installed. The only coupling is the table.

    pip install PyMySQL

The crawler picks the removal up at the start of its next pass; it is not
listening, so this returns as soon as the row is committed.
"""

from __future__ import annotations

from typing import Any

import pymysql


def connect(host: str, user: str, password: str, *, port: int = 3306,
            database: str = "scrapev3") -> Any:
    """Connect to the shared state database.

    `autocommit=False` with an explicit commit per write, so a failed removal
    leaves nothing half-written. A silently swallowed failure here means a
    publisher who asked to be removed stays in the crawl.
    """
    return pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, charset="utf8mb4", autocommit=False,
    )


def remove_agency(conn: Any, a_id: int, note: str | None = None) -> None:
    """Record an agency as removed.

    Idempotent: `a_id` is the primary key, so submitting the same removal twice
    updates the note rather than failing. The caller never has to check first.

    `a_id` is the agency id, not a domain. One domain can carry hundreds of
    agencies - house.gov carries 417 - so removing a domain would take all of
    them with it.
    """
    if not isinstance(a_id, int) or a_id <= 0:
        raise ValueError(f"a_id must be a positive integer, got {a_id!r}")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO removed_agency (a_id, removed_at, note) "
            "VALUES (%s, UTC_TIMESTAMP(), %s) "
            "ON DUPLICATE KEY UPDATE note = VALUES(note)",
            (a_id, note),
        )
    conn.commit()


def removed_agencies(conn: Any) -> list[tuple[int, Any, str | None]]:
    """Agencies already removed, newest first."""
    with conn.cursor() as cur:
        cur.execute("SELECT a_id, removed_at, note FROM removed_agency "
                    "ORDER BY removed_at DESC")
        return [(int(r[0]), r[1], r[2]) for r in cur.fetchall()]


def is_removed(conn: Any, a_id: int) -> bool:
    """Has this agency already been removed?"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM removed_agency WHERE a_id = %s", (a_id,))
        return cur.fetchone() is not None


if __name__ == "__main__":
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("a_id", type=int)
    ap.add_argument("--note")
    ap.add_argument("--host", default=os.environ.get("SCRAPEV3_DB_HOST", "localhost"))
    ap.add_argument("--user", default=os.environ.get("SCRAPEV3_DB_USER", "website"))
    args = ap.parse_args()

    password = os.environ.get("SCRAPEV3_DB_PASSWORD")
    if not password:
        sys.exit("Set SCRAPEV3_DB_PASSWORD")

    conn = connect(args.host, args.user, password)
    try:
        remove_agency(conn, args.a_id, args.note)
        print(f"a_id {args.a_id} recorded as removed. "
              "It is purged on the crawler's next pass.")
    finally:
        conn.close()
