"""Removing an agency, everywhere, on request.

A publisher asks to be taken out. The request arrives at the website codebase,
which is a separate project on a separate machine; both already speak to the
same MySQL, so that is the whole integration surface. The site inserts one row
and the crawler acts on it at the start of its next pass.

Nothing listens. `crawl_once` already reads state before it acquires anything -
a removal is a write to shared state, not an event to subscribe to - so there is
no service to run, no port to hold open, and no shared Python environment
between the two codebases.

**A list, not a queue.** The obvious shape is a queue the crawler drains, and it
breaks the moment there is a second crawler: the first to drain marks the row
consumed and the second never applies it, leaving that agency crawled on one
machine and not the other. Silently, which is the failure mode this project
exists to catch. So the table is the *current set of removed agencies*, and each
pass makes local state match it. Idempotent, self-healing after downtime, and
correct with one crawler or five.

**The tombstone is the point.** `seed` upserts every row of the source CSV, so
deleting an agency's rows is undone by the next seed. The list is what makes a
removal permanent: the crawler purges against it and `seed` skips it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .settings import Settings
from .tracing import get as _get_logger

if TYPE_CHECKING:                   # the TNS sink is optional; importing it is not
    from .frontier import Frontier
    from .sink import Sink
    from .tns import TnsSink

log = _get_logger(__name__)

# Lives in the state schema, never in `tns`. That database is the newswire CMS
# and its shape is not ours to change; `scrapev3` is where crawler state goes.
_DDL = """
CREATE TABLE IF NOT EXISTS removed_agency (
  a_id        INT NOT NULL,
  removed_at  DATETIME NOT NULL,
  note        VARCHAR(255) NULL,
  PRIMARY KEY (a_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


@dataclass
class RemovalReport:
    """What a removal actually deleted, per store.

    Counts rather than log text, so the CLI can print one table and tests can
    assert on numbers.
    """

    a_id: int
    targets: int = 0            # frontier: newsroom URLs
    domains: int = 0            # frontier: domains left with no targets at all
    indexed: int = 0            # dedup index rows
    archived: int = 0           # JSONL records
    files: int = 0              # JSONL files rewritten
    press_releases: int = 0     # tns.press_release rows
    errors: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        """Rows removed across every store. Zero means nothing was there."""
        return (self.targets + self.domains + self.indexed
                + self.archived + self.press_releases)


# ---------------------------------------------------------------------------
# The shared list
# ---------------------------------------------------------------------------

# MySQL's "Unknown database" - the first-run case, not a misconfiguration.
_ER_BAD_DB_ERROR = 1049
_SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def connect(settings: Settings) -> Any:
    """Open the state database, creating the schema on first use.

    `ensure_table` creates the table but cannot create the database it lives in,
    and connecting to a database that does not exist fails before any statement
    can run. Rather than making the operator do one step by hand, the first
    connection creates it - the crawler already creates its own tables, and a
    schema is the same kind of thing.
    """
    from .tns import connect as _connect

    database = settings.mysql.state_db
    try:
        return _connect(settings, database)
    except Exception as exc:                                # noqa: BLE001
        if getattr(exc, "args", (None,))[0] != _ER_BAD_DB_ERROR:
            raise

    if not _SAFE_IDENT.match(database):
        raise RuntimeError(
            f"SCRAPEV3_MYSQL_STATE_DB={database!r} is not a plain identifier; "
            "refusing to interpolate it into CREATE DATABASE")

    conn = _connect(settings, None)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` "
                        "CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()
    log.debug("created the %s database on first use", database)
    return _connect(settings, database)


def ensure_table(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def add(conn: Any, a_id: int, note: str | None = None) -> None:
    """Record an agency as removed. Idempotent - asking twice is not an error."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO removed_agency (a_id, removed_at, note) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE note = VALUES(note)",
            (a_id, datetime.utcnow(), note),
        )
    conn.commit()


def drop(conn: Any, a_id: int) -> int:
    """Take an agency off the list. Does not restore anything already deleted."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM removed_agency WHERE a_id = %s", (a_id,))
        return cur.rowcount


def listed(conn: Any) -> set[int]:
    """Every removed agency id."""
    with conn.cursor() as cur:
        cur.execute("SELECT a_id FROM removed_agency")
        return {int(r[0]) for r in cur.fetchall()}


def rows(conn: Any) -> list[tuple[int, Any, str | None]]:
    """The list with its metadata, newest first, for display."""
    with conn.cursor() as cur:
        cur.execute("SELECT a_id, removed_at, note FROM removed_agency "
                    "ORDER BY removed_at DESC")
        return [(int(r[0]), r[1], r[2]) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------

def remove(a_id: int, *, frontier: "Frontier", sink: "Sink",
           tns: "TnsSink | None" = None) -> RemovalReport:
    """Delete one agency from every store that holds it.

    Ordered cheapest-and-most-reversible first, ending with the archive rewrite,
    which is the only step that cannot be undone. A failure in any one store is
    recorded and the rest still run: a half-removed agency is worse than one
    removed everywhere except MySQL, and the next reconcile finishes the job
    because every step here is idempotent.
    """
    report = RemovalReport(a_id=a_id)

    try:
        report.targets, report.domains = frontier.remove_agency(a_id)
    except Exception as exc:                                # noqa: BLE001
        report.errors.append(f"frontier: {type(exc).__name__}: {exc}")

    try:
        report.indexed = sink.remove_agency(a_id)
    except Exception as exc:                                # noqa: BLE001
        report.errors.append(f"index: {type(exc).__name__}: {exc}")

    if tns is not None:
        try:
            report.press_releases = tns.delete_rows([a_id])
        except Exception as exc:                            # noqa: BLE001
            report.errors.append(f"press_release: {type(exc).__name__}: {exc}")

    try:
        report.archived, report.files = sink.purge_archive(a_id)
    except Exception as exc:                                # noqa: BLE001
        report.errors.append(f"archive: {type(exc).__name__}: {exc}")

    log.debug("removed a_id=%s targets=%d domains=%d indexed=%d archived=%d "
              "press_release=%d", a_id, report.targets, report.domains,
              report.indexed, report.archived, report.press_releases)
    return report


def reconcile(a_ids: set[int], *, frontier: "Frontier", sink: "Sink",
              tns: "TnsSink | None" = None) -> list[RemovalReport]:
    """Make local state match the list.

    Every agency on the list is purged on every pass, not just newly added
    ones - that is what makes this self-healing. Agencies already gone cost one
    empty SELECT each and are not reported, so a long list stays cheap.
    """
    reports = []
    for a_id in sorted(a_ids):
        report = remove(a_id, frontier=frontier, sink=sink, tns=tns)
        if report.touched or report.errors:
            reports.append(report)
    return reports
