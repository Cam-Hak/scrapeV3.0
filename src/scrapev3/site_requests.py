"""Adding a site, on request, from the other codebase.

The mirror of `removal.py`, and deliberately the same shape. That table lets the
website say *stop crawling this*; this one lets it say *start*. Same integration
surface - one table in the shared `scrapev3` schema, no API, no socket, no shared
filesystem, no shared Python environment - and the same rules, because both are
read in the same breath by the same pass.

**A list, not a queue.** Draining breaks the moment there is a second crawler:
the first to consume the row seeds the site and the second never does, so the
site is crawled on one machine and not the other, silently. So the table is the
*current set of requested sites*, and each pass makes the frontier match it.
`upsert_sites` deliberately does not reset scheduling state, so re-applying a
request already in the frontier costs one upsert and changes nothing.

**Removal outranks a request.** The website owns both tables, and without a rule
they fight: an agency on `removed_agency` that also appears here would be seeded
again on every pass, quietly undoing the removal a publisher asked for. So a
request whose agency is on the removal list is refused - and counted, because a
refusal nobody can see is the same as no rule at all.

**The domain is ours to derive.** The website sends an agency id and a URL and
nothing else. The registrable domain is the pacing key and the shard key, and a
value supplied from outside that disagreed with `registrable_domain` would
either split one publisher across two workers or hammer it - neither loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable

from .removal import connect as connect            # re-exported: same database
from .settings import Settings
from .tracing import get as _get_logger

if TYPE_CHECKING:
    from .frontier import Frontier

log = _get_logger(__name__)

# Keyed on the newsroom URL alone, exactly like `target` - the table this one
# seeds. Not on (a_id, newsroom_url): the frontier cannot hold one URL under two
# agencies, so a key that allowed it here would let the list express a state
# nothing downstream can reach. It would also not fit - VARCHAR(768) utf8mb4 is
# 3072 bytes, InnoDB's whole key limit, so the a_id pushes it over.
#
# An agency with several newsrooms still gets several rows; it is the URL that
# is unique, not the publisher.
_DDL = """
CREATE TABLE IF NOT EXISTS requested_site (
  newsroom_url  VARCHAR(768) NOT NULL,
  a_id          INT NOT NULL,
  requested_at  DATETIME NOT NULL,
  note          VARCHAR(255) NULL,
  PRIMARY KEY (newsroom_url),
  KEY idx_requested_agency (a_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


@dataclass
class RequestReport:
    """What one reconcile pass did with the list.

    Counts rather than log text, so the CLI prints one table and tests assert on
    numbers. `seeded` is targets upserted, which for a site already in the
    frontier is a no-op that still counts - the list is re-applied whole, so the
    number says how many the frontier was made to match, not how many are new.
    """

    seeded: int = 0
    refused: list[int] = field(default_factory=list)   # a_ids on the removal list
    invalid: list[str] = field(default_factory=list)   # URLs we could not resolve

    @property
    def touched(self) -> bool:
        return bool(self.seeded or self.refused or self.invalid)


# ---------------------------------------------------------------------------
# The shared list
# ---------------------------------------------------------------------------

def ensure_table(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def add(conn: Any, a_id: int, newsroom_url: str, note: str | None = None) -> None:
    """Request a site. Idempotent - asking twice is not an error.

    Re-requesting a URL under a different `a_id` moves it, rather than failing:
    the frontier holds one owner per newsroom, so correcting which agency a page
    belongs to has to be expressible.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requested_site (newsroom_url, a_id, requested_at, note) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE a_id = VALUES(a_id), note = VALUES(note)",
            (newsroom_url, a_id, datetime.utcnow(), note),
        )
    conn.commit()


def drop(conn: Any, a_id: int, newsroom_url: str | None = None) -> int:
    """Take a request off the list. Does not un-seed anything already applied.

    Withdrawing is an operator action, which is why the website's grant has no
    DELETE: a page that could retract a request could retract someone else's.
    """
    with conn.cursor() as cur:
        if newsroom_url is None:
            cur.execute("DELETE FROM requested_site WHERE a_id = %s", (a_id,))
        else:
            cur.execute("DELETE FROM requested_site WHERE a_id = %s "
                        "AND newsroom_url = %s", (a_id, newsroom_url))
        return cur.rowcount


def listed(conn: Any) -> list[tuple[int, str]]:
    """Every requested (a_id, newsroom_url), for applying."""
    with conn.cursor() as cur:
        cur.execute("SELECT a_id, newsroom_url FROM requested_site")
        return [(int(r[0]), r[1]) for r in cur.fetchall()]


def rows(conn: Any) -> list[tuple[int, str, Any, str | None]]:
    """The list with its metadata, newest first, for display."""
    with conn.cursor() as cur:
        cur.execute("SELECT a_id, newsroom_url, requested_at, note "
                    "FROM requested_site ORDER BY requested_at DESC")
        return [(int(r[0]), r[1], r[2], r[3]) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------

def reconcile(requests: Iterable[tuple[int, str]], *, frontier: "Frontier",
              removed: set[int] | None = None) -> RequestReport:
    """Make the frontier hold every requested site. Safe to run every pass.

    The whole list is re-applied, never drained, so two crawlers cannot each
    consume the other's requests and a crawler that was down catches up by
    running normally rather than by anyone replaying anything.

    A URL that will not resolve to a registrable domain is reported and skipped
    rather than raised: one malformed row from a web form must not stop the
    other requests, and it stays on the list so the report names it again next
    pass instead of vanishing after one complaint nobody read.
    """
    from .urls import canonical_url, registrable_domain

    removed = removed or set()
    report = RequestReport()
    to_seed: list[tuple[int, str, str]] = []

    for a_id, raw in sorted(requests):
        if a_id in removed:
            # Refused, not skipped: this is the two tables disagreeing, and the
            # count is how anyone finds out that the website is asking for an
            # agency it has also asked to remove.
            report.refused.append(a_id)
            continue
        url = canonical_url(raw)
        domain = registrable_domain(url) if url else ""
        if not url or not domain:
            report.invalid.append(raw)
            continue
        to_seed.append((a_id, url, domain))

    if to_seed:
        report.seeded = frontier.upsert_sites(to_seed)

    if report.refused:
        log.warning("refused %d requested site(s) whose agency is on the "
                    "removal list: %s", len(report.refused),
                    ", ".join(str(a) for a in sorted(set(report.refused))))
    if report.invalid:
        log.warning("%d requested site(s) had no usable URL: %s",
                    len(report.invalid), ", ".join(report.invalid[:5]))
    return report


def pending(settings: Settings) -> list[tuple[int, str]]:
    """The list, or an empty one if the state database is unreachable.

    Used by `seed`, which must still load `data/sites.csv` when MySQL is not
    configured - a request list nobody can read is a reason to seed less, not a
    reason to seed nothing.
    """
    try:
        conn = connect(settings)
    except Exception as exc:                                # noqa: BLE001
        log.debug("no requested-site list: %s", exc)
        return []
    try:
        ensure_table(conn)
        return listed(conn)
    finally:
        conn.close()
