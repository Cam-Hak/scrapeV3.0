"""Where extracted articles go.

JSONL plus a SQLite dedup index: the archive, and the record of what has been
seen. `tns/` writes the production row on top of this; the two are separate
because a scrape is a fact while a load is an action that can fail, and only
one of them should be retried.

Dedup deliberately does NOT use the v2 filename key
(`$H <prefix><YYMMDD><headline[-10:]>`). That key was both the CMS display
contract and the dedup key, so two same-day articles whose headlines happened to
end alike collided silently, and an editor fixing a headline caused a re-insert.
The filename is still generated identically for downstream - by
`tns.record.tns_filename`, in one place, so the two cannot drift apart - but
dedup runs on canonical-URL hash plus content hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .extract.models import Article
from .tns.record import tns_filename
from .urls import canonical_url

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def content_hash(body: str | None) -> str:
    """Hash of the normalised body, so trivial whitespace edits do not
    masquerade as new articles."""
    normalised = _NON_ALNUM.sub(" ", (body or "").lower()).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


_DDL = """
CREATE TABLE IF NOT EXISTS article (
  url_hash       TEXT PRIMARY KEY,
  content_hash   TEXT NOT NULL,
  domain         TEXT NOT NULL,
  a_id           INTEGER,
  url            TEXT NOT NULL,
  headline       TEXT,
  published_at   TEXT,
  body_len       INTEGER,
  first_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_article_content ON article (content_hash);
CREATE INDEX IF NOT EXISTS idx_article_domain  ON article (domain, first_seen_at);
"""

# Added with the MySQL sink. Kept as a migration rather than folded into _DDL
# above so an existing index survives the upgrade with its history intact.
#
#   tns_state  NULL      never attempted
#              loaded    in tns.press_release
#              rejected  will never load, and we know why (too short, no agency)
#              error     the attempt failed and should be retried
#
# This column exists because the dedup index is written BEFORE the MySQL
# insert, so without it a transient database hiccup would mark an article seen
# and it would never be offered again. That is exactly v2's fail-closed dedup,
# where any DB error silently dropped the article. `scrapev3 tns backfill`
# replays everything not yet loaded.
_MIGRATIONS = (
    ("tns_state", "ALTER TABLE article ADD COLUMN tns_state TEXT"),
    ("tns_filename", "ALTER TABLE article ADD COLUMN tns_filename TEXT"),
    ("tns_at", "ALTER TABLE article ADD COLUMN tns_at TEXT"),
)

_TNS_INDEX = "CREATE INDEX IF NOT EXISTS idx_article_tns ON article (tns_state)"


class Sink:
    """JSONL output plus a SQLite dedup index."""

    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        (self.data_dir / "articles").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "articles.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_DDL)
        self._migrate()
        self._fh = None
        self._path = (self.data_dir / "articles" /
                      f"articles-{datetime.now().strftime('%Y%m%d')}.jsonl")

    def _migrate(self) -> None:
        have = {row[1] for row in self.db.execute("PRAGMA table_info(article)")}
        for column, ddl in _MIGRATIONS:
            if column not in have:
                self.db.execute(ddl)
        self.db.execute(_TNS_INDEX)

    # -- dedup ----------------------------------------------------------

    def seen_url(self, url: str) -> bool:
        """Cheap pre-fetch check. Ordering matters: this runs BEFORE the
        expensive article fetch, which is the one thing v2 got right here."""
        row = self.db.execute(
            "SELECT 1 FROM article WHERE url_hash = ?", (url_hash(url),)).fetchone()
        return row is not None

    def seen_content(self, body: str | None) -> str | None:
        """Return the URL we already hold this body under, if any.

        Press releases syndicate verbatim across dozens of outlets, so the same
        text legitimately arrives from many URLs.
        """
        row = self.db.execute(
            "SELECT url FROM article WHERE content_hash = ? LIMIT 1",
            (content_hash(body),)).fetchone()
        return row[0] if row else None

    # -- writing --------------------------------------------------------

    def write(self, article: Article, *, domain: str, a_id: int | None = None,
              agency_prefix: str = "") -> bool:
        """Persist one article. Returns False if it was a duplicate."""
        uh = url_hash(article.url)
        ch = content_hash(article.body)
        now = datetime.utcnow().isoformat(timespec="seconds")

        try:
            self.db.execute(
                "INSERT INTO article (url_hash, content_hash, domain, a_id, url, "
                "headline, published_at, body_len, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uh, ch, domain, a_id, article.url, article.headline,
                 article.date.value.isoformat() if article.date.value else None,
                 article.body_len, now),
            )
        except sqlite3.IntegrityError:
            return False        # already have this URL

        record: dict[str, Any] = {
            "url": article.url,
            "domain": domain,
            "a_id": a_id,
            "headline": article.headline,
            "body": article.body,
            "published_at": article.date.value.isoformat() if article.date.value else None,
            "date_precision": article.date.precision.value,
            "date_source": article.date.source.value,
            "date_raw": article.date.raw,
            "headline_source": article.headline_source.value,
            "body_source": article.body_source.value,
            "language": article.language,
            "url_hash": uh,
            "content_hash": ch,
            # Only when the agency directory supplied a real prefix. An empty
            # one produces a plausible-looking "$H 260826..." that is not the
            # filename anything will actually be stored under.
            "tns_filename": (tns_filename(agency_prefix, article.date.value, article.headline)
                             if agency_prefix else None),
            "quality": article.quality,
            "warnings": article.warnings,
            "scraped_at": now,
        }
        if self._fh is None:
            self._fh = self._path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()        # crash-safe: a killed run keeps what it got
        return True

    # -- MySQL load state -----------------------------------------------

    def mark_tns(self, url: str, state: str, filename: str | None = None) -> None:
        """Record what happened when this article was offered to `press_release`.

        Called after the row is already in the dedup index, which is the whole
        point: without it a failed insert would leave the article marked seen
        and unreachable forever.
        """
        self.db.execute(
            "UPDATE article SET tns_state = ?, tns_filename = ?, tns_at = ? "
            "WHERE url_hash = ?",
            (state, filename, datetime.utcnow().isoformat(timespec="seconds"),
             url_hash(url)),
        )

    def loaded_tns(self) -> list[tuple[str, str]]:
        """(url, filename) for every article this index believes was loaded."""
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT url, tns_filename FROM article "
            "WHERE tns_state = 'loaded' AND tns_filename IS NOT NULL")]

    def reset_tns(self, urls: list[str]) -> int:
        """Forget that these were loaded, so they are offered again.

        For when MySQL is reset underneath us - a TRUNCATE, a restore from a
        dump, a schema rebuild. The index is a cache of what the database
        contains, and a cache that cannot be invalidated is a trap.
        """
        self.db.executemany(
            "UPDATE article SET tns_state = NULL, tns_filename = NULL, tns_at = NULL "
            "WHERE url = ?", [(u,) for u in urls])
        return len(urls)

    def forget(self, *, domain: str | None = None, a_id: int | None = None) -> int:
        """Drop articles from the dedup index so they are fetched again.

        `seen_url` runs BEFORE the article fetch, so an article this index
        remembers is never re-crawled - correct in production, and the reason
        a re-run after truncating MySQL otherwise stores nothing at all.

        The JSONL archive is left alone. It is append-only on purpose; a
        re-crawl adds a fresh line rather than rewriting history. The single
        exception is `purge_archive`, used when an agency is removed on
        request - there the history going is the point.
        """
        if domain:
            sql, params = "DELETE FROM article WHERE domain = ?", (domain,)
        elif a_id is not None:
            sql, params = "DELETE FROM article WHERE a_id = ?", (a_id,)
        else:
            sql, params = "DELETE FROM article", ()
        return self.db.execute(sql, params).rowcount

    def article_stats(self, *, since: datetime) -> dict[int, tuple[int, int, str | None, str | None]]:
        """Per-agency article totals, recent volume, and two timestamps.

        Returns a_id -> (total, since `since`, latest published_at, latest
        first_seen_at).

        The two dates answer different questions and both are needed. The
        publisher's date says whether the newsroom is still publishing; ours
        says when this agency's crawl last demonstrably worked, which is the
        only evidence left when the frontier's own bookkeeping is missing.

        A total on its own only ever goes up, so a feed that broke six months
        ago still shows a healthy-looking number; the windowed count is what
        makes a stalled site visible. Both come from one grouped scan.

        `published_at` is the publisher's own date, not `first_seen_at`: the
        question the grid answers is whether the newsroom is still publishing,
        which our crawl timestamps cannot tell apart from our own schedule.
        Rows with no a_id (an unmatched agency) are skipped rather than bucketed
        under a fake id.

        The timestamp comes back as the stored ISO string rather than a parsed
        datetime. Dates arrive here from two backends in three formats, and one
        parser that knows about all of them (`status._as_dt`) is safer than a
        second one here that quietly disagrees with it.
        """
        rows = self.db.execute(
            "SELECT a_id, COUNT(*), "
            "       SUM(CASE WHEN COALESCE(published_at, first_seen_at) >= ? "
            "                THEN 1 ELSE 0 END), "
            "       MAX(published_at), MAX(first_seen_at) "
            "FROM article WHERE a_id IS NOT NULL GROUP BY a_id",
            (since.isoformat(sep="T", timespec="seconds"),),
        ).fetchall()
        return {int(a_id): (int(total), int(recent or 0), published, seen)
                for a_id, total, recent, published, seen in rows}

    def remove_agency(self, a_id: int) -> int:
        """Delete one agency's rows from the dedup index.

        Distinct from `forget`, which drops rows so the articles are fetched
        *again*. This drops them because the agency is gone, and is paired with
        `purge_archive` so the index and the archive do not disagree about what
        exists.
        """
        return self.db.execute(
            "DELETE FROM article WHERE a_id = ?", (a_id,)).rowcount

    def purge_archive(self, a_id: int) -> tuple[int, int]:
        """Rewrite every daily JSONL without this agency's records.

        Returns (records removed, files rewritten).

        The one irreversible step in a removal, and the one exception to the
        archive being append-only: `forget` leaves it alone precisely so a
        re-crawl appends rather than rewriting history, but a removal request
        is a request for the history to go.

        Written to a temporary file in the same directory and moved into place,
        so an interrupted run leaves the original intact rather than a
        half-written archive. `os.replace` is atomic within a filesystem.
        """
        # Close the day's append handle FIRST. Windows refuses to replace a
        # file that still has an open handle, so doing this afterwards makes
        # every purge fail with PermissionError - caught upstream and recorded,
        # leaving the archive silently intact. `write` reopens lazily.
        if self._fh is not None:
            self._fh.close()
            self._fh = None

        removed = files = 0
        for path in sorted((self.data_dir / "articles").glob("articles-*.jsonl")):
            kept: list[str] = []
            dropped = 0
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        if json.loads(line).get("a_id") == a_id:
                            dropped += 1
                            continue
                    except json.JSONDecodeError:
                        pass        # keep anything unparseable rather than lose it
                    kept.append(line)
            if not dropped:
                continue

            tmp = path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as out:
                out.writelines(kept)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
            removed += dropped
            files += 1

        return removed, files

    def pending_tns(self, limit: int | None = None) -> list[tuple[str, str, int | None]]:
        """Articles never loaded, or whose load failed. Returns (url, domain, a_id)."""
        sql = ("SELECT url, domain, a_id FROM article "
               "WHERE tns_state IS NULL OR tns_state = 'error' "
               "ORDER BY first_seen_at")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(r[0], r[1], r[2]) for r in self.db.execute(sql)]

    @property
    def path(self) -> Path:
        return self._path

    def stats(self) -> dict[str, Any]:
        total = self.db.execute("SELECT COUNT(*) FROM article").fetchone()[0]
        domains = self.db.execute("SELECT COUNT(DISTINCT domain) FROM article").fetchone()[0]
        dupes = self.db.execute(
            "SELECT COUNT(*) FROM (SELECT content_hash FROM article "
            "GROUP BY content_hash HAVING COUNT(*) > 1)").fetchone()[0]
        by_state = dict(self.db.execute(
            "SELECT COALESCE(tns_state, 'not attempted'), COUNT(*) "
            "FROM article GROUP BY 1"))
        return {"articles": total, "domains": domains, "body_text_dupes": dupes,
                "tns": by_state}

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self.db.close()
