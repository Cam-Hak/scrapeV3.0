"""The `tns.press_release` sink.

Writes the production row. The INSERT is v2's, column for column, with
`location` added - see `record.py` for why the shape is not ours to choose.

This sink does **not** own deduplication. `Sink` (JSONL + SQLite) already
decided this article is new, on canonical-URL and content hashes, before the
article was even fetched. What arrives here is expected to be novel, so a
`filename` collision means something specific and worth handling rather than
swallowing:

* **the same document, already loaded** - by an earlier v3 run whose SQLite
  index has since been rebuilt, or by v2. Detected by comparing the stored
  `a_id` and headline, and skipped.
* **two different documents whose filenames happen to match** - same agency,
  same day, headlines ending alike. This is v2's silent data-loss bug: the
  second document was dropped and counted as a duplicate. Here the filename is
  widened - more of the headline's tail, which is exactly what the per-site
  `FILENAME CHARS` column existed to do by hand - until it is unique.

Failure is per-article and never fatal. One oversized body, one agency missing
from the directory, one dropped connection: the article is counted in a named
bucket and the crawl continues. A run that quietly wrote nothing is the worst
possible outcome, so `stats()` reports every bucket and the CLI prints them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .agencies import AgencyDirectory, _safe_ident
from .record import (DEFAULT_STATUS, MIN_WORDS, SHORT_DOC_MAX_WORDS,
                     PressRelease, Rejected, build_press_release)

_INSERT = """
INSERT INTO {db}.press_release
  (headline, content_date, body_txt, a_id, status,
   create_date, last_action, filename, headline2, uname, location)
VALUES (%s, %s, %s, %s, %s, SYSDATE(), SYSDATE(), %s, %s, %s, %s)
"""

_LOOKUP = "SELECT a_id, headline FROM {db}.press_release WHERE filename = %s"

# How far the filename tail is widened on a genuine collision. v2 exposed the
# first two of these as a per-site column and used them on 2 of 2,405 sites.
_GOBACK_LADDER = (10, 15, 20, 25, 30, 40)

_DUPLICATE_KEY = 1062


@dataclass
class TnsStats:
    """One bucket per outcome. Everything that does not insert lands in one."""

    inserted: int = 0
    duplicate: int = 0          # already in press_release, same document
    widened: int = 0            # filename collision resolved, still inserted
    short_doc: int = 0          # inserted with status W for editor review
    no_agency: int = 0          # a_id absent from the directory
    no_uname: int = 0           # inserted, but nobody owns it
    no_lede: int = 0
    no_headline: int = 0
    too_short: int = 0
    too_long: int = 0
    insert_error: int = 0
    errors: list[str] = field(default_factory=list)

    def bump(self, bucket: str) -> None:
        setattr(self, bucket, getattr(self, bucket, 0) + 1)


class TnsSink:
    """Insert into `tns.press_release`. Not thread-safe; one per crawl."""

    def __init__(
        self,
        conn: Any,
        agencies: AgencyDirectory,
        *,
        db: str = "tns",
        status: str = DEFAULT_STATUS,
        goback_chars: int = 10,
        min_words: int = MIN_WORDS,
        short_doc_max_words: int = SHORT_DOC_MAX_WORDS,
        dry_run: bool = False,
    ):
        self.conn = conn
        self.agencies = agencies
        self.db = _safe_ident(db)
        self.status = status
        self.goback_chars = goback_chars
        self.min_words = min_words
        self.short_doc_max_words = short_doc_max_words
        self.dry_run = dry_run
        self.stats = TnsStats()
        self._insert_sql = _INSERT.format(db=self.db)
        self._lookup_sql = _LOOKUP.format(db=self.db)
        # Kept for --dry-run, so a run can be inspected without a write.
        self.pending: list[PressRelease] = []
        # The filename the last successful insert actually landed under,
        # which is not row.filename when a collision forced a widening.
        self.last_filename: str | None = None

    # -- composition ----------------------------------------------------

    def build(self, *, a_id: int, headline: str, body: str,
              published: datetime | date, url: str) -> PressRelease | Rejected:
        agency = self.agencies.get(a_id)
        if agency is None:
            return Rejected("no_agency", f"a_id {a_id} not in tns.agencies")

        return build_press_release(
            a_id=a_id,
            prefix=agency.prefix,
            lede_template=agency.lede,
            uname=agency.uname,
            headline=headline,
            body=body,
            published=published.date() if isinstance(published, datetime) else published,
            url=url,
            status=self.status,
            goback_chars=self.goback_chars,
            min_words=self.min_words,
            short_doc_max_words=self.short_doc_max_words,
        )

    # -- writing --------------------------------------------------------

    def load(self, *, a_id: int, headline: str, body: str,
             published: datetime | date, url: str) -> str:
        """Compose and insert one article. Returns the outcome bucket's name.

        A name rather than a boolean because the caller has to tell three
        things apart: loaded, will never load, and failed-and-should-be-retried.
        Collapsing those is how v2 lost articles to transient database errors.
        """
        row = self.build(a_id=a_id, headline=headline, body=body,
                         published=published, url=url)
        if isinstance(row, Rejected):
            self.stats.bump(row.reason)
            if row.detail:
                self.stats.errors.append(f"{row.reason}: a_id {a_id} {row.detail}")
            return row.reason
        return self.insert(row)

    def insert(self, row: PressRelease) -> str:
        """Insert a composed row, widening the filename past a collision."""
        if row.status == "W" and row.headline2 == "short doc":
            self.stats.short_doc += 1
        if not row.uname or row.uname == "-1":
            self.stats.no_uname += 1

        if self.dry_run:
            self.last_filename = row.filename
            self.pending.append(row)
            self.stats.inserted += 1
            return "inserted"

        attempt = row
        for widening, goback in enumerate(_GOBACK_LADDER):
            if widening:
                wider = row.with_filename_width(goback)
                if wider.filename == attempt.filename:
                    # The headline is already shorter than the tail we are
                    # asking for, so no width separates these two documents.
                    self.stats.duplicate += 1
                    self.stats.errors.append(
                        f"duplicate: a_id {row.a_id} filename "
                        f"{attempt.filename!r} cannot be widened further")
                    return "duplicate"
                attempt = wider

            try:
                self._execute(attempt)
            except Exception as exc:                        # noqa: BLE001
                if not _is_duplicate_key(exc):
                    self.stats.insert_error += 1
                    self.stats.errors.append(
                        f"insert_error: a_id {row.a_id} {type(exc).__name__}: {exc}")
                    return "insert_error"
                if self._is_same_document(attempt.filename, row):
                    self.stats.duplicate += 1
                    return "duplicate"
                continue

            self.last_filename = attempt.filename
            self.stats.inserted += 1
            if widening:
                self.stats.widened += 1
            return "inserted"

        self.stats.duplicate += 1
        self.stats.errors.append(
            f"duplicate: a_id {row.a_id} still colliding at "
            f"{_GOBACK_LADDER[-1]} headline chars")
        return "duplicate"

    def delete_rows(self, a_ids: list[int] | None) -> int:
        """Remove rows so a test run can be repeated. Returns the count.

        `a_ids=None` means every row and is spelled out at the call site, never
        reached by an unset argument falling through - deleting the whole table
        because a scope was empty is exactly the accident worth designing out.
        An empty list deletes nothing.

        DELETE rather than TRUNCATE: it can be scoped, it reports how many rows
        went, and it leaves `pr_id` climbing so a re-run never reuses an id
        something downstream may already have seen.
        """
        if a_ids is not None and not a_ids:
            return 0
        with self.conn.cursor() as cur:
            if a_ids is None:
                cur.execute(f"DELETE FROM {self.db}.press_release")
            else:
                placeholders = ", ".join(["%s"] * len(a_ids))
                cur.execute(f"DELETE FROM {self.db}.press_release "
                            f"WHERE a_id IN ({placeholders})", tuple(a_ids))
            return cur.rowcount

    def missing_filenames(self, filenames: list[str], *, batch: int = 500) -> list[str]:
        """Which of these are NOT in press_release right now.

        Batched against the unique index rather than scanning the table, so the
        cost tracks how many articles we hold locally, not how many rows the
        newswire has accumulated over the years.
        """
        missing: list[str] = []
        for i in range(0, len(filenames), batch):
            chunk = filenames[i:i + batch]
            placeholders = ", ".join(["%s"] * len(chunk))
            with self.conn.cursor() as cur:
                cur.execute(
                    f"SELECT filename FROM {self.db}.press_release "
                    f"WHERE filename IN ({placeholders})", tuple(chunk))
                present = {r[0] for r in cur.fetchall()}
            missing.extend(f for f in chunk if f not in present)
        return missing

    def _execute(self, row: PressRelease) -> None:
        with self.conn.cursor() as cur:
            cur.execute(self._insert_sql, (
                row.headline, row.content_date, row.body_txt, row.a_id,
                row.status, row.filename, row.headline2, row.uname, row.location,
            ))

    def _is_same_document(self, filename: str, row: PressRelease) -> bool:
        """Is the row already holding this filename the same document?

        Agency plus headline is decisive here: two documents from one agency on
        one day with byte-identical headlines are the same document.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(self._lookup_sql, (filename,))
                existing = cur.fetchone()
        except Exception:                                   # noqa: BLE001
            # Cannot tell - treat as a collision and widen. Widening a filename
            # is recoverable; dropping an article is not.
            return False
        if not existing:
            return False
        return int(existing[0] or 0) == row.a_id and (existing[1] or "") == row.headline

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:                                   # noqa: BLE001
            pass


def _is_duplicate_key(exc: Exception) -> bool:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0] == _DUPLICATE_KEY
    return "1062" in str(exc) or "Duplicate entry" in str(exc)
