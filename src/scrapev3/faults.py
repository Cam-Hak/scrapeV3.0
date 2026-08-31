"""What went wrong, kept past the end of the run, and ranked.

`failure_kind` already says what a fetch failed of, and `owner_of` says whose
problem that is. Neither survived the process: `CrawlStats` was built, printed
as eight strings truncated to 110 characters, and discarded. So the corpus-wide
questions - is this new, is it spreading, which of these is mine - could only be
answered by re-running the crawl and watching.

**Breadth is the multiplier, not frequency.** One site 404ing forty URLs and
twenty sites failing once each are the same total and completely different
problems; the second is a defect in our code that twenty publishers are
demonstrating. Ranking on occurrences puts the first at the top, which is why
`by_failure` sorted that way has never once pointed at the right thing.

**`policy` is weighted zero.** A robots refusal and a bot wall are recorded in
full, with every domain attributed, and never rank. The README already says it -
"those are sites declining an identified crawler, which is their call" - and
this is that sentence as arithmetic. Without it, 27 robots refusals and 8 walls
sit permanently above every defect we could actually fix.

Nothing here invents a word. The vocabulary is `fetch.FAILURE_KINDS`, and
`severity` and `owner` are derived from it at read time rather than stored, so a
row written last month cannot disagree with today's rules - the property
`audit --rescore` has, and the reason `57b81b4` stored `unreachable_kind`
instead of a verdict.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .fetch import FAILURE_KINDS, owner_of, severity_of
from .tracing import get as _get_logger

log = _get_logger(__name__)

# `us` outranks `site` at equal breadth because a defect we can fix should come
# before a site oddity that may have no fix at all. `policy` is zero, not small:
# no amount of breadth should lift a refusal we are obeying into a work queue.
_OWNER_WEIGHT = {"us": 3.0, "site": 1.0, "policy": 0.0}

# A severity-3 kind we own, on two domains: 3 * 2 * 3 = 18. Two unrelated sites
# hitting the same defect in our code is the point at which it stops being one
# site's quirk, and it is the cheapest thing on the board to fix.
URGENT_AT = 18.0

# Severity 2 at site weight across five domains: 2 * 5 * 1 = 10. Five
# publishers behaving the same way is a pattern a workaround can target; below
# that the workaround costs more than it returns.
NOTABLE_AT = 10.0

# Runs of history kept. At daily cadence this is a month, which is the window in
# which "is this new?" is still answerable - a kind that appeared once in March
# is noise, not history. At roughly 600 rows per run the whole store stays under
# 20k rows, so a full scan is instant and no index tuning is ever needed.
KEEP_RUNS = 30

# Enough of the exception message to recognise it; the rest is the URL, which
# `sample_url` already carries.
_DETAIL_MAX = 200

_DDL = """
CREATE TABLE IF NOT EXISTS fault_run (
  run_id     TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at   TEXT,
  command    TEXT NOT NULL,
  scope      TEXT,
  domains    INTEGER NOT NULL DEFAULT 0,
  targets    INTEGER NOT NULL DEFAULT 0,
  stats_json TEXT
);

CREATE TABLE IF NOT EXISTS fault (
  run_id        TEXT NOT NULL,
  kind          TEXT NOT NULL,
  domain        TEXT NOT NULL,
  a_id          INTEGER,
  n             INTEGER NOT NULL DEFAULT 0,
  first_at      TEXT NOT NULL,
  last_at       TEXT NOT NULL,
  sample_url    TEXT,
  sample_detail TEXT,
  PRIMARY KEY (run_id, kind, domain)
);

CREATE INDEX IF NOT EXISTS idx_fault_kind   ON fault (kind, last_at);
CREATE INDEX IF NOT EXISTS idx_fault_domain ON fault (domain, last_at);
"""


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def attention(kind: str, domains: int, occurrences: int = 0) -> float:
    """How much this kind deserves, across the corpus.

    `occurrences` is accepted and deliberately unused in the score - it is the
    tiebreak and a displayed column, never a multiplier. See the module
    docstring for why.
    """
    return severity_of(kind) * domains * _OWNER_WEIGHT.get(owner_of(kind), 1.0)


def band(score: float) -> str:
    """`urgent`, `notable` or `minor`.

    Note the asymmetry with `severity_of`, which resolves an unknown kind to 3.
    A *band* fails quiet: something unranked showing as urgent would train
    people to skim the top of the list, which is the one thing the list cannot
    survive.
    """
    if score >= URGENT_AT:
        return "urgent"
    if score >= NOTABLE_AT:
        return "notable"
    return "minor"


def worst_domains(rows: Iterable["FaultRow"], limit: int = 10) -> list[tuple[str, int, list[str]]]:
    """Domains ranked by how many DIFFERENT ways they failed.

    Distinct kinds, not occurrences: a site failing five different ways is more
    broken than one failing the same way fifty times. `audit.TargetAudit.score`
    sums severities for exactly this reason; this is that idea on the domain
    axis rather than the target one.

    `policy` kinds are excluded outright - a domain is not "bad" for having a
    robots.txt we obey.
    """
    by_domain: dict[str, set[str]] = {}
    for row in rows:
        if owner_of(row.kind) == "policy":
            continue
        by_domain.setdefault(row.domain, set()).add(row.kind)
    scored = [(d, sum(severity_of(k) for k in kinds), sorted(kinds))
              for d, kinds in by_domain.items()]
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:limit]


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

@dataclass
class FaultRow:
    """One kind, on one domain, in one run."""

    run_id: str
    kind: str
    domain: str
    a_id: int | None = None
    n: int = 0
    first_at: str = ""
    last_at: str = ""
    sample_url: str | None = None
    sample_detail: str | None = None

    @property
    def severity(self) -> int:
        return severity_of(self.kind)

    @property
    def owner(self) -> str:
        return owner_of(self.kind)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, with the derived fields spelled out.

        They are computed, never stored - but a consumer reading the JSON has
        no classifier, so they are written here.
        """
        return {"run_id": self.run_id, "kind": self.kind, "domain": self.domain,
                "a_id": self.a_id, "n": self.n, "first_at": self.first_at,
                "last_at": self.last_at, "sample_url": self.sample_url,
                "sample_detail": self.sample_detail,
                "severity": self.severity, "owner": self.owner}


@dataclass
class Tally:
    """One kind, rolled up across the domains that raised it."""

    kind: str
    domains: list[str] = field(default_factory=list)
    occurrences: int = 0
    sample_url: str | None = None
    sample_detail: str | None = None

    @property
    def severity(self) -> int:
        return severity_of(self.kind)

    @property
    def owner(self) -> str:
        return owner_of(self.kind)

    @property
    def score(self) -> float:
        return attention(self.kind, len(self.domains), self.occurrences)

    @property
    def band(self) -> str:
        return band(self.score)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity,
                "owner": self.owner, "domains": len(self.domains),
                "occurrences": self.occurrences, "score": round(self.score, 1),
                "band": self.band, "example_domain": self.domains[0] if self.domains else None,
                "sample_detail": self.sample_detail}


def tally(rows: Iterable[FaultRow]) -> list[Tally]:
    """Roll rows up per kind, ranked worst first.

    Occurrences break ties and never contribute to the score.
    """
    out: dict[str, Tally] = {}
    for row in rows:
        t = out.setdefault(row.kind, Tally(kind=row.kind))
        if row.domain not in t.domains:
            t.domains.append(row.domain)
        t.occurrences += row.n
        if t.sample_detail is None:
            t.sample_url, t.sample_detail = row.sample_url, row.sample_detail
    for t in out.values():
        t.domains.sort()
    return sorted(out.values(), key=lambda t: (-t.score, -t.occurrences, t.kind))


def summarise(rows: Iterable[FaultRow]) -> dict[str, Any]:
    """The header line: how much of this is even ours."""
    rows = list(rows)
    by_owner: dict[str, int] = {"us": 0, "site": 0, "policy": 0}
    for row in rows:
        by_owner[row.owner] = by_owner.get(row.owner, 0) + row.n
    return {"kinds": len({r.kind for r in rows}),
            "domains": len({r.domain for r in rows}),
            "occurrences": sum(r.n for r in rows),
            "owner": by_owner}


def to_json(rows: list[FaultRow], run: dict[str, Any] | None = None) -> str:
    """The same shape the CLI prints, for a script that would rather not parse a table."""
    return json.dumps({
        "generated_at": _now(),
        "run": run or {},
        "summary": summarise(rows),
        "ranked": [t.as_dict() for t in tally(rows)],
        "worst_domains": [{"domain": d, "score": s, "kinds": k}
                          for d, s, k in worst_domains(rows)],
        "faults": [r.as_dict() for r in rows],
    }, indent=2)


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds")


def new_run_id() -> str:
    """The stamp shape `data/audits/` already uses, so the two sort together."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class FaultStore:
    """Per-run fault aggregates, in their own SQLite file.

    Deliberately not `articles.sqlite`. That is the dedup index, and `reset`,
    `forget()` and `purge_archive()` bulk-delete from it - you reset *because*
    something was wrong, and deleting the evidence in the same command is
    backwards. Deliberately not MySQL either: this has to work on a laptop with
    no database configured, which is where diagnostics are needed most.

    Aggregated per `(run_id, kind, domain)`. A pass over 2,400 targets produces
    thousands of occurrences whose only use is being counted; the two questions
    this store exists to answer are "how many" and "which sites", and a count
    per domain answers both. One sample URL and detail per row is the whole
    difference between "40 x http_4xx on hccs.edu" and being able to go and
    look at one. Per-URL forensics is what `--debug` already gives, live, at no
    storage cost.
    """

    def __init__(self, data_dir: str | Path = "data"):
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path / "faults.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_DDL)

    def close(self) -> None:
        self.db.close()

    def start_run(self, run_id: str, *, command: str, scope: str | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO fault_run (run_id, started_at, command, scope) "
            "VALUES (?, ?, ?, ?)", (run_id, _now(), command, scope))

    def finish_run(self, run_id: str, *, domains: int = 0, targets: int = 0,
                   stats: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "UPDATE fault_run SET ended_at = ?, domains = ?, targets = ?, "
            "stats_json = ? WHERE run_id = ?",
            (_now(), domains, targets,
             json.dumps(stats) if stats is not None else None, run_id))

    def record(self, run_id: str, kind: str, domain: str, *, n: int = 1,
               a_id: int | None = None, url: str | None = None,
               detail: str | None = None) -> None:
        """Add occurrences of one kind on one domain.

        `a_id` is stored but is NOT part of the key. `house.gov` carries 417
        agencies and a fetch fault is a property of the domain - the pacing and
        blame unit - not of whichever legislator page happened to hit it. In the
        key it would explode into 417 rows saying the same thing. The column is
        the first `a_id` seen, kept as a join handle, and it is lossy on purpose.
        """
        now = _now()
        self.db.execute(
            "INSERT INTO fault (run_id, kind, domain, a_id, n, first_at, "
            "last_at, sample_url, sample_detail) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, kind, domain) DO UPDATE SET "
            "  n = n + excluded.n, last_at = excluded.last_at",
            (run_id, kind, domain, a_id, n, now, now, url,
             (detail or "")[:_DETAIL_MAX] or None))

    def rows(self, *, run_id: str | None = None, kind: str | None = None,
             domain: str | None = None, owner: str | None = None,
             runs: int = 1) -> list[FaultRow]:
        """Faults from the most recent `runs` runs, or one named run.

        `owner` is filtered in Python, not SQL, because it is derived from
        `kind` and deliberately has no column - filtering it in SQL would mean
        storing it.
        """
        where, params = [], []
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        else:
            ids = self.run_ids(limit=runs)
            if not ids:
                return []
            where.append(f"run_id IN ({', '.join('?' * len(ids))})")
            params += ids
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if domain:
            where.append("domain = ?")
            params.append(domain)

        sql = ("SELECT run_id, kind, domain, a_id, n, first_at, last_at, "
               "sample_url, sample_detail FROM fault WHERE "
               + " AND ".join(where) + " ORDER BY kind, domain")
        out = [FaultRow(*r) for r in self.db.execute(sql, params).fetchall()]
        if owner:
            out = [r for r in out if r.owner == owner]
        return out

    def run_ids(self, limit: int = 1) -> list[str]:
        """The newest run ids, newest first."""
        return [r[0] for r in self.db.execute(
            "SELECT run_id FROM fault_run ORDER BY started_at DESC, run_id DESC "
            "LIMIT ?", (limit,)).fetchall()]

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT run_id, started_at, ended_at, command, scope, domains, "
            "targets FROM fault_run WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return dict(zip(("run_id", "started_at", "ended_at", "command", "scope",
                         "domains", "targets"), row))

    def prune(self, keep: int = KEEP_RUNS) -> int:
        """Drop everything outside the newest `keep` runs. Returns rows deleted."""
        ids = self.run_ids(limit=keep)
        if not ids:
            return 0
        marks = ", ".join("?" * len(ids))
        deleted = self.db.execute(
            f"DELETE FROM fault WHERE run_id NOT IN ({marks})", ids).rowcount
        self.db.execute(f"DELETE FROM fault_run WHERE run_id NOT IN ({marks})", ids)
        return deleted
