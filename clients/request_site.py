"""Request a site for the scrapev3 crawl, from another Python codebase.

The mirror of `remove_agency.py`. Copy this file into the site; it deliberately
imports nothing from scrapev3 - no shared virtualenv, no file paths, no
dependency on the crawler being installed. The only coupling is the table.

    pip install PyMySQL

The crawler seeds the request at the start of its next pass; it is not
listening, so this returns as soon as the row is committed.

A removal outranks a request. If the agency is on `removed_agency`, the crawler
refuses this request every pass rather than resurrecting it - so a publisher who
asked to be taken out stays out even if a form here asks for them back.
"""

from __future__ import annotations

import re
from typing import Any

import pymysql

_HTTP = re.compile(r"^https?://", re.I)


def connect(host: str, user: str, password: str, *, port: int = 3306,
            database: str = "scrapev3") -> Any:
    """Connect to the shared state database.

    `autocommit=False` with an explicit commit per write, so a failed request
    leaves nothing half-written. A silently swallowed failure here means a site
    somebody asked for is never crawled and nobody finds out.
    """
    return pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, charset="utf8mb4", autocommit=False,
    )


def request_site(conn: Any, a_id: int, newsroom_url: str,
                 note: str | None = None) -> None:
    """Ask for a newsroom URL to be crawled.

    Idempotent: `newsroom_url` is the primary key, so submitting the same
    request twice updates the note rather than failing. The caller never has to
    check first. Re-requesting the same URL under a different `a_id` moves it -
    the crawler holds one owner per newsroom, so correcting that has to be
    possible.

    Send the newsroom page - the index that lists press releases - not one
    article and not the site's home page. Discovery starts from this URL and
    works outward, so a home page makes it guess and an article gives it nothing
    to guess from.

    Do not send a domain. The crawler derives the registrable domain itself,
    because that value is its pacing and shard key: one supplied from outside
    that disagreed would either split a publisher across two workers or hammer
    it, and neither fails loudly.
    """
    if not isinstance(a_id, int) or a_id <= 0:
        raise ValueError(f"a_id must be a positive integer, got {a_id!r}")

    newsroom_url = (newsroom_url or "").strip()
    # Checked here rather than left to the crawler because the person who can
    # fix a typo is the one submitting it, and they are standing right here. An
    # unusable URL that reaches the table is reported once a pass into a log
    # nobody is reading.
    if not _HTTP.match(newsroom_url):
        raise ValueError(f"newsroom_url must be an http(s) URL, got {newsroom_url!r}")
    if len(newsroom_url) > 768:
        raise ValueError("newsroom_url is longer than the column (768)")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requested_site (newsroom_url, a_id, requested_at, note) "
            "VALUES (%s, %s, UTC_TIMESTAMP(), %s) "
            "ON DUPLICATE KEY UPDATE a_id = VALUES(a_id), note = VALUES(note)",
            (newsroom_url, a_id, note),
        )
    conn.commit()


def requested_sites(conn: Any) -> list[tuple[int, str, Any, str | None]]:
    """Sites already requested, newest first.

    Being on this list does not mean the site is being crawled. It means it has
    been asked for; read `agency_status` to find out what happened.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT a_id, newsroom_url, requested_at, note "
                    "FROM requested_site ORDER BY requested_at DESC")
        return [(int(r[0]), r[1], r[2], r[3]) for r in cur.fetchall()]


def is_requested(conn: Any, a_id: int, newsroom_url: str) -> bool:
    """Has this newsroom URL already been requested for this agency?"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM requested_site WHERE a_id = %s "
                    "AND newsroom_url = %s", (a_id, (newsroom_url or "").strip()))
        return cur.fetchone() is not None


if __name__ == "__main__":
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("a_id", type=int)
    ap.add_argument("newsroom_url")
    ap.add_argument("--note")
    ap.add_argument("--host", default=os.environ.get("SCRAPEV3_DB_HOST", "localhost"))
    ap.add_argument("--user", default=os.environ.get("SCRAPEV3_DB_USER", "website"))
    args = ap.parse_args()

    password = os.environ.get("SCRAPEV3_DB_PASSWORD")
    if not password:
        sys.exit("Set SCRAPEV3_DB_PASSWORD")

    conn = connect(args.host, args.user, password)
    try:
        request_site(conn, args.a_id, args.newsroom_url, args.note)
        print(f"a_id {args.a_id} {args.newsroom_url} recorded as requested. "
              "It is seeded on the crawler's next pass.")
    finally:
        conn.close()
