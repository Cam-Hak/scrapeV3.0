"""The domain-lease frontier.

The central design decision of the whole system: **the queue hands out DOMAIN
LEASES, not URLs.**

If it handed out URLs, two workers could independently receive two URLs from
example.com and hit it simultaneously. Politeness would then require a
distributed rate limiter (Redis token buckets, a lock server) - chatty,
fragile, and it *fails open* during a partition, which is exactly when we would
look like a DoS source.

Handing out domain leases makes politeness a mutual-exclusion property. One
worker owns a domain for the lease duration and paces itself locally against a
monotonic clock. No coordination. It *fails closed*: a partitioned worker's
lease simply expires, and until it does nobody else may touch that domain.

Secondary win: the lease-holder fetches all of that domain's new articles over
one keepalive connection - one TCP+TLS handshake instead of N, which is both
faster for us and gentler on the origin.

Two backends, one behaviour:
  * SQLite  - the default while no MySQL is provisioned. Single-writer, so the
              lease is a plain IMMEDIATE transaction.
  * MySQL   - production. Uses SELECT ... FOR UPDATE SKIP LOCKED (8.0.1+) so
              concurrent workers never block each other or collide.
"""

from __future__ import annotations

import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import (EPOCH, FOREVER, DomainRecord, FrontierStats, Target,
                     from_ts, to_ts, utcnow)
from .shard import shard_for

# Column order shared by both backends, so row->record mapping is identical.
_COLUMNS = (
    "domain", "a_id", "newsroom_url", "shard", "enabled", "next_allowed_at",
    "leased_until", "lease_owner", "crawl_delay_s", "revisit_period_s",
    "consec_failures", "discovery_method", "feed_url", "etag", "last_modified",
    "needs_browser", "needs_browser_at", "last_success_at", "p50_body_len",
)
_COLUMN_LIST = ", ".join(_COLUMNS)

_TARGET_COLUMNS = (
    "newsroom_url", "domain", "a_id", "enabled", "discovery_method", "feed_url",
    "etag", "last_modified", "last_success_at", "consec_failures", "p50_body_len",
    "feed_absent", "probed_at",
)
_TARGET_COLUMN_LIST = ", ".join(_TARGET_COLUMNS)


def _to_target(row):
    d = dict(zip(_TARGET_COLUMNS, row))
    return Target(
        newsroom_url=d["newsroom_url"],
        domain=d["domain"],
        a_id=int(d["a_id"]),
        enabled=bool(d["enabled"]),
        discovery_method=d["discovery_method"],
        feed_url=d["feed_url"],
        etag=d["etag"],
        last_modified=d["last_modified"],
        last_success_at=from_ts(d["last_success_at"]),
        consec_failures=int(d["consec_failures"]),
        p50_body_len=int(d["p50_body_len"]) if d["p50_body_len"] is not None else None,
        feed_absent=bool(d["feed_absent"]),
        probed_at=from_ts(d["probed_at"]),
    )


def _to_record(row: Sequence[Any]) -> DomainRecord:
    d = dict(zip(_COLUMNS, row))
    return DomainRecord(
        domain=d["domain"],
        a_id=int(d["a_id"]),
        newsroom_url=d["newsroom_url"],
        shard=int(d["shard"]),
        enabled=bool(d["enabled"]),
        next_allowed_at=from_ts(d["next_allowed_at"]) or utcnow(),
        leased_until=from_ts(d["leased_until"]),
        lease_owner=d["lease_owner"],
        crawl_delay_s=float(d["crawl_delay_s"]),
        revisit_period_s=int(d["revisit_period_s"]),
        consec_failures=int(d["consec_failures"]),
        discovery_method=d["discovery_method"],
        feed_url=d["feed_url"],
        etag=d["etag"],
        last_modified=d["last_modified"],
        needs_browser=bool(d["needs_browser"]),
        needs_browser_at=from_ts(d["needs_browser_at"]),
        last_success_at=from_ts(d["last_success_at"]),
        p50_body_len=int(d["p50_body_len"]) if d["p50_body_len"] is not None else None,
    )


class Frontier(ABC):
    """Backend-agnostic frontier operations."""

    placeholder = "?"

    # The frontier does two separable jobs, and only one of them is optional.
    #
    #   The LEASE - one worker owns a domain at a time - is the mechanism the
    #   politeness guarantee rests on. It is never negotiable.
    #
    #   The SCHEDULE - `next_allowed_at`, the revisit period, failure backoff -
    #   decides *when* a domain comes round again. Useful in production, pure
    #   friction while prototyping: a site crawled once is then unreachable for
    #   24 hours, so "re-run that site" silently does nothing.
    #
    # With this False the schedule is ignored: every enabled domain is due, and
    # releasing one leaves it due. Per-host delay, per-IP caps and the lease all
    # still apply, so a crawl is exactly as polite - it is the cadence between
    # passes that stops being enforced here, and becomes whatever invokes the
    # crawler.
    schedule_enabled = True

    @abstractmethod
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]: ...

    @abstractmethod
    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None: ...

    @abstractmethod
    def _transaction(self): ...

    @abstractmethod
    def create_schema(self) -> None: ...

    @abstractmethod
    def _upsert_sql(self) -> str: ...

    def agency_ids(self) -> list[int]:
        """Every agency id in the target list.

        The output sink needs this to report how much of the corpus it can
        actually resolve to a `tns.agencies` row - a gap that is invisible
        until something asks.
        """
        return [int(r[0]) for r in self._execute("SELECT DISTINCT a_id FROM target")
                if r[0] is not None]

    @abstractmethod
    def _upsert_target_sql(self) -> str: ...

    @abstractmethod
    def _select_due_sql(self, limit: int) -> str: ...

    def close(self) -> None:
        pass

    def _ph(self, n: int) -> str:
        return ", ".join([self.placeholder] * n)

    # -- seeding --------------------------------------------------------

    def upsert_sites(self, rows: Iterable[tuple[int, str, str]]) -> int:
        """Insert or refresh targets as (a_id, newsroom_url, domain).

        Deliberately does NOT reset scheduling state on re-seed: re-importing
        the site list must not wipe next_allowed_at, etag, or failure counts.
        """
        rows = list(rows)
        if not rows:
            return 0
        now = to_ts(utcnow())

        # One domain_state row per registrable domain - the lease and pacing
        # unit. Several targets may collapse onto the same one.
        by_domain: dict[str, tuple[int, str]] = {}
        for a_id, url, domain in rows:
            by_domain.setdefault(domain, (int(a_id), url))
        self._executemany(
            self._upsert_sql(),
            [(dom, aid, url, shard_for(dom), now) for dom, (aid, url) in by_domain.items()],
        )

        # One target row per newsroom URL - what actually gets crawled.
        self._executemany(
            self._upsert_target_sql(),
            [(url, domain, int(a_id)) for a_id, url, domain in rows],
        )
        return len(rows)

    def targets_for(self, domain: str) -> list[Target]:
        """Every enabled newsroom URL on this domain."""
        rows = self._execute(
            "SELECT {cols} FROM target WHERE domain = {p} AND enabled = 1 "
            "ORDER BY newsroom_url".format(cols=_TARGET_COLUMN_LIST, p=self.placeholder),
            (domain,),
        )
        return [_to_target(r) for r in rows]

    def release_target(
        self,
        newsroom_url: str,
        *,
        success: bool,
        discovery_method: str | None = None,
        feed_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        p50_body_len: int | None = None,
        feed_absent: bool | None = None,
    ) -> None:
        """Record the outcome for a single newsroom URL.

        Scheduling lives on the domain (that is the pacing unit); this tracks
        per-target discovery state and health so one dead legislator page does
        not look like a dead house.gov.
        """
        now = utcnow()
        sets: dict[str, Any] = {}
        if success:
            sets["consec_failures"] = 0
            sets["last_success_at"] = to_ts(now)
        else:
            rows = self._execute(
                "SELECT consec_failures FROM target WHERE newsroom_url = {p}".format(
                    p=self.placeholder), (newsroom_url,))
            sets["consec_failures"] = (int(rows[0][0]) if rows else 0) + 1
        for key, value in (
            ("discovery_method", discovery_method),
            ("feed_url", feed_url),
            ("etag", etag),
            ("last_modified", last_modified),
            ("p50_body_len", p50_body_len),
        ):
            if value is not None:
                sets[key] = value
        if feed_absent is not None:
            # Stamp the probe time alongside the verdict so it can expire.
            sets["feed_absent"] = 1 if feed_absent else 0
            sets["probed_at"] = to_ts(now)
        assignments = ", ".join("{k} = {p}".format(k=k, p=self.placeholder) for k in sets)
        self._execute(
            "UPDATE target SET {a} WHERE newsroom_url = {p}".format(
                a=assignments, p=self.placeholder),
            (*sets.values(), newsroom_url),
        )

    # -- leasing --------------------------------------------------------

    def acquire(
        self,
        worker_id: str,
        *,
        shard_lo: int = 0,
        shard_hi: int = 1023,
        limit: int = 32,
        lease_seconds: int = 600,
    ) -> list[DomainRecord]:
        """Atomically lease up to `limit` due domains in this shard range.

        Ordering by next_allowed_at makes this index scan the durable,
        shardable equivalent of the Mercator min-heap.
        """
        now = utcnow()
        now_ts = to_ts(now)
        until_ts = to_ts(now + timedelta(seconds=lease_seconds))

        # Two independent conditions share this timestamp: "is it due yet"
        # and "is the lease free". Turning the schedule off relaxes only the
        # first, by comparing against a bound nothing can exceed.
        due_ts = now_ts if self.schedule_enabled else FOREVER

        with self._transaction():
            candidates = self._execute(
                self._select_due_sql(limit), (shard_lo, shard_hi, due_ts, now_ts)
            )
            domains = [r[0] for r in candidates]
            if not domains:
                return []
            self._execute(
                "UPDATE domain_state SET leased_until = {p}, lease_owner = {p} "
                "WHERE domain IN ({holes})".format(
                    p=self.placeholder, holes=self._ph(len(domains))
                ),
                (until_ts, worker_id, *domains),
            )
            rows = self._execute(
                "SELECT {cols} FROM domain_state WHERE domain IN ({holes})".format(
                    cols=_COLUMN_LIST, holes=self._ph(len(domains))
                ),
                tuple(domains),
            )
        records = [_to_record(r) for r in rows]
        for rec in records:
            rec.targets = self.targets_for(rec.domain)
        return records

    def forget_discovery(self, *, domain: str | None = None,
                         a_id: int | None = None) -> int:
        """Drop what a target learned about where its articles come from.

        The frontier caches the winning source so a solved domain skips the
        cascade - the difference between one request and fourteen. The cost is
        that a *wrong* answer is just as sticky as a right one: a target that
        once cached a site-wide feed goes straight back to it every run, and
        the cascade never gets a chance to reconsider.

        So resetting a site for testing has to forget this too, or the re-crawl
        faithfully reproduces the previous run's mistake. Conditional-GET state
        goes with it, since an unchanged ETag would skip the fetch entirely.
        """
        p = self.placeholder
        if domain:
            where, params = f"domain = {p}", [domain]
        elif a_id is not None:
            where, params = f"a_id = {p}", [a_id]
        else:
            where, params = "1 = 1", []

        affected = len(self._execute(f"SELECT newsroom_url FROM target WHERE {where}",
                                     params))
        with self._transaction():
            self._execute(
                "UPDATE target SET discovery_method = NULL, feed_url = NULL, "
                "etag = NULL, last_modified = NULL, feed_absent = 0, "
                f"probed_at = NULL WHERE {where}", params)
            self._execute(
                "UPDATE domain_state SET discovery_method = NULL, feed_url = NULL, "
                f"etag = NULL, last_modified = NULL WHERE {where}", params)
        return affected

    def domains_for(self, *, a_id: int) -> list[str]:
        """Every registrable domain carrying a target for this agency."""
        return [r[0] for r in self._execute(
            f"SELECT DISTINCT domain FROM target WHERE a_id = {self.placeholder}",
            (a_id,))]

    def acquire_domains(self, worker_id: str, domains: Sequence[str], *,
                        lease_seconds: int = 600) -> list[DomainRecord]:
        """Lease named domains regardless of when they are next due.

        `acquire` serves the schedule, ordered by `next_allowed_at`; asking for
        one site by name has to bypass that queue or it never reaches the front
        - 1,747 never-crawled domains sort ahead of anything re-crawled today.

        It bypasses the *schedule* only. The lease still applies, so a domain
        another worker holds is skipped rather than crawled twice, and per-host
        pacing is untouched: a targeted crawl is exactly as polite as a
        scheduled one.
        """
        domains = list(dict.fromkeys(domains))          # de-dupe, keep order
        if not domains:
            return []
        now = utcnow()
        now_ts = to_ts(now)
        until_ts = to_ts(now + timedelta(seconds=lease_seconds))
        holes = self._ph(len(domains))
        p = self.placeholder

        with self._transaction():
            free = [r[0] for r in self._execute(
                f"SELECT domain FROM domain_state WHERE domain IN ({holes}) "
                f"AND enabled = 1 AND leased_until < {p}",
                (*domains, now_ts))]
            if not free:
                return []
            self._execute(
                f"UPDATE domain_state SET leased_until = {p}, lease_owner = {p} "
                f"WHERE domain IN ({self._ph(len(free))})",
                (until_ts, worker_id, *free))
            rows = self._execute(
                f"SELECT {_COLUMN_LIST} FROM domain_state "
                f"WHERE domain IN ({self._ph(len(free))})", tuple(free))

        records = [_to_record(r) for r in rows]
        for rec in records:
            rec.targets = self.targets_for(rec.domain)
        return records

    def release(
        self,
        domain: str,
        *,
        success: bool,
        crawl_delay_s: float | None = None,
        discovery_method: str | None = None,
        feed_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        needs_browser: bool | None = None,
        p50_body_len: int | None = None,
    ) -> None:
        """Release a lease and schedule the next visit.

        On success the domain is rescheduled one revisit period out. On failure
        it backs off exponentially, so dead domains fade instead of being
        retried forever - v2 had 21 dnsNotFound errors and no such mechanism.
        """
        rows = self._execute(
            "SELECT {cols} FROM domain_state WHERE domain = {p}".format(
                cols=_COLUMN_LIST, p=self.placeholder
            ),
            (domain,),
        )
        if not rows:
            return
        rec = _to_record(rows[0])

        now = utcnow()
        if success:
            rec.consec_failures = 0
            delay = rec.revisit_period_s
            last_success = to_ts(now)
        else:
            rec.consec_failures += 1
            delay = rec.backoff_seconds()
            last_success = to_ts(rec.last_success_at) if rec.last_success_at else None

        # `consec_failures` is still counted with the schedule off - it is the
        # health signal `frontier` reports on - it just no longer defers.
        if not self.schedule_enabled:
            delay = 0

        sets: dict[str, Any] = {
            "leased_until": EPOCH,
            "lease_owner": None,
            "next_allowed_at": to_ts(now + timedelta(seconds=delay)),
            "consec_failures": rec.consec_failures,
            "last_success_at": last_success,
        }
        for key, value in (
            ("crawl_delay_s", crawl_delay_s),
            ("discovery_method", discovery_method),
            ("feed_url", feed_url),
            ("etag", etag),
            ("last_modified", last_modified),
            ("p50_body_len", p50_body_len),
        ):
            if value is not None:
                sets[key] = value
        if needs_browser is not None:
            sets["needs_browser"] = 1 if needs_browser else 0
            sets["needs_browser_at"] = to_ts(now)

        assignments = ", ".join(
            "{k} = {p}".format(k=k, p=self.placeholder) for k in sets
        )
        self._execute(
            "UPDATE domain_state SET {a} WHERE domain = {p}".format(
                a=assignments, p=self.placeholder
            ),
            (*sets.values(), domain),
        )

    def release_expired_leases(self) -> int:
        """Reclaim leases from workers that died mid-crawl.

        This is what makes the system fail *closed*: a wedged worker's domains
        become available again only after its lease expires, never sooner.
        """
        now_ts = to_ts(utcnow())
        p = self.placeholder
        rows = self._execute(
            "SELECT domain FROM domain_state "
            "WHERE leased_until > {p} AND leased_until < {p}".format(p=p),
            (EPOCH, now_ts),
        )
        if not rows:
            return 0
        domains = [r[0] for r in rows]
        self._execute(
            "UPDATE domain_state SET leased_until = {p}, lease_owner = NULL "
            "WHERE domain IN ({holes})".format(p=p, holes=self._ph(len(domains))),
            (EPOCH, *domains),
        )
        return len(domains)

    def make_due(self, *, domain: str | None = None, a_id: int | None = None) -> int:
        """Bring domains forward so they can be crawled again immediately.

        A normal release pushes `next_allowed_at` out by the revisit period, so
        without this a domain crawled today is unreachable until tomorrow -
        which makes iterating on one site impossible. Only for development;
        production cadence is the revisit period doing its job.

        Politeness is unaffected: this moves the *schedule*, not the per-host
        delay or the lease, both of which still apply on the next pass.
        """
        p = self.placeholder
        if domain:
            where, params = f"domain = {p}", [domain]
        elif a_id is not None:
            # a_id lives per target, so scope through the target table.
            where, params = f"domain IN (SELECT domain FROM target WHERE a_id = {p})", [a_id]
        else:
            where, params = "1 = 1", []

        affected = self._execute(f"SELECT domain FROM domain_state WHERE {where}", params)
        with self._transaction():
            self._execute(
                f"UPDATE domain_state SET next_allowed_at = {p}, leased_until = {p}, "
                f"lease_owner = NULL, consec_failures = 0 WHERE {where}",
                [to_ts(utcnow()), EPOCH, *params])
        return len(affected)

    def status_rows(self) -> list[tuple]:
        """Per-target state, with the domain-level signals attached.

        One query rather than a call per agency: this runs over every target
        the crawler holds (2,401 of them) to build the website's grid, and a
        round trip each would make publishing status cost more than the crawl.

        `needs_browser` is on `domain_state`, not `target`, because a site that
        renders its articles with JavaScript does so for the whole site - so it
        is joined in rather than duplicated. LEFT, so a target seeded but never
        leased still gets a row instead of vanishing from the grid.

        The first nine columns are positional in `status.compose`, so anything
        new goes on the end. The tail is the cached-discovery and scheduling
        state: whether this target is solved and how, whether a conditional GET
        is armed, and when the schedule comes back for it. Selected here rather
        than in a second query because this one already scans every target.
        """
        return self._execute(
            "SELECT t.a_id, t.domain, t.newsroom_url, t.enabled, "
            "       t.discovery_method, t.last_success_at, t.consec_failures, "
            "       t.p50_body_len, d.needs_browser, "
            "       t.feed_url, t.feed_absent, t.probed_at, t.etag, "
            "       t.last_modified, d.next_allowed_at, d.crawl_delay_s, "
            "       d.revisit_period_s "
            "FROM target t LEFT JOIN domain_state d ON d.domain = t.domain "
            "ORDER BY t.a_id, t.newsroom_url")

    def remove_agency(self, a_id: int) -> tuple[int, int]:
        """Delete one agency's targets, and any domain left with none.

        Returns (targets removed, domains removed).

        The orphan check is the whole subtlety. `domain_state` is keyed on the
        registrable domain and carries the pacing, lease and learned-discovery
        state for every agency sharing it - house.gov carries 417. Deleting the
        domain row because one of its agencies left would throw away the other
        416 and their cached discovery sources. So a domain goes only when the
        last target on it does.

        Deliberately a delete, not `disable`: a removal request is not a pause,
        and a row left behind with `enabled = 0` is a row somebody can turn back
        on. The permanent record lives in the tombstone list, which `seed`
        consults - see `removal.py`.
        """
        p = self.placeholder
        targets = [r[0] for r in self._execute(
            f"SELECT newsroom_url FROM target WHERE a_id = {p}", (a_id,))]
        if not targets:
            return 0, 0
        domains = [r[0] for r in self._execute(
            f"SELECT DISTINCT domain FROM target WHERE a_id = {p}", (a_id,))]

        with self._transaction():
            self._execute(f"DELETE FROM target WHERE a_id = {p}", (a_id,))
            orphans = [r[0] for r in self._execute(
                f"SELECT domain FROM domain_state WHERE domain IN "
                f"({self._ph(len(domains))}) "
                f"AND domain NOT IN (SELECT domain FROM target)", tuple(domains))]
            if orphans:
                self._execute(
                    f"DELETE FROM domain_state WHERE domain IN "
                    f"({self._ph(len(orphans))})", tuple(orphans))
        return len(targets), len(orphans)

    def disable(self, domain: str) -> None:
        self._execute(
            "UPDATE domain_state SET enabled = 0 WHERE domain = {p}".format(
                p=self.placeholder
            ),
            (domain,),
        )

    def get(self, domain: str) -> DomainRecord | None:
        rows = self._execute(
            "SELECT {cols} FROM domain_state WHERE domain = {p}".format(
                cols=_COLUMN_LIST, p=self.placeholder
            ),
            (domain,),
        )
        return _to_record(rows[0]) if rows else None

    def stats(self) -> FrontierStats:
        now_ts = to_ts(utcnow())
        p = self.placeholder
        sql = (
            "SELECT COUNT(*), "
            "SUM(CASE WHEN enabled THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN enabled AND next_allowed_at <= {p} "
            "         AND leased_until < {p} THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN leased_until > {p} THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN consec_failures > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN last_success_at IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN needs_browser THEN 1 ELSE 0 END) "
            "FROM domain_state"
        ).format(p=p)
        # Same relaxation as `acquire`, so the report cannot contradict what a
        # crawl would actually lease.
        due_ts = now_ts if self.schedule_enabled else FOREVER
        row = self._execute(sql, (due_ts, now_ts, EPOCH))[0]
        total, *rest = (int(v or 0) for v in row)
        n_targets = int(
            self._execute("SELECT COUNT(*) FROM target WHERE enabled = 1")[0][0] or 0
        )
        return FrontierStats(total, n_targets, *rest)


# ---------------------------------------------------------------------------
# SQLite - the default until MySQL is provisioned
# ---------------------------------------------------------------------------

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS domain_state (
  domain            TEXT PRIMARY KEY,
  a_id              INTEGER NOT NULL,
  newsroom_url      TEXT NOT NULL,
  shard             INTEGER NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 1,
  next_allowed_at   TEXT NOT NULL,
  leased_until      TEXT NOT NULL DEFAULT '1970-01-01 00:00:00',
  lease_owner       TEXT,
  crawl_delay_s     REAL NOT NULL DEFAULT 5.0,
  revisit_period_s  INTEGER NOT NULL DEFAULT 86400,
  consec_failures   INTEGER NOT NULL DEFAULT 0,
  discovery_method  TEXT,
  feed_url          TEXT,
  etag              TEXT,
  last_modified     TEXT,
  needs_browser     INTEGER NOT NULL DEFAULT 0,
  needs_browser_at  TEXT,
  last_success_at   TEXT,
  p50_body_len      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lease
  ON domain_state (shard, next_allowed_at, enabled, leased_until);

CREATE TABLE IF NOT EXISTS target (
  newsroom_url      TEXT PRIMARY KEY,
  domain            TEXT NOT NULL,
  a_id              INTEGER NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 1,
  discovery_method  TEXT,
  feed_url          TEXT,
  etag              TEXT,
  last_modified     TEXT,
  last_success_at   TEXT,
  consec_failures   INTEGER NOT NULL DEFAULT 0,
  p50_body_len      INTEGER,
  feed_absent       INTEGER NOT NULL DEFAULT 0,
  probed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_target_domain ON target (domain, enabled);
"""


class SQLiteFrontier(Frontier):
    placeholder = "?"

    def __init__(self, path: str | Path = "data/frontier.sqlite"):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._depth = 0

    def create_schema(self) -> None:
        self.conn.executescript(_SQLITE_DDL)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self.conn.executemany(sql, [tuple(r) for r in rows])

    def _transaction(self):
        store = self

        class _Txn:
            def __enter__(self):
                if store._depth == 0:
                    # IMMEDIATE takes the write lock up front, so two workers
                    # cannot both read the same due rows and then both claim.
                    store.conn.execute("BEGIN IMMEDIATE")
                store._depth += 1

            def __exit__(self, exc_type, *_):
                store._depth -= 1
                if store._depth == 0:
                    store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Txn()

    def _upsert_sql(self) -> str:
        return (
            "INSERT INTO domain_state (domain, a_id, newsroom_url, shard, next_allowed_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET "
            "  a_id = excluded.a_id, newsroom_url = excluded.newsroom_url, "
            "  shard = excluded.shard"
        )

    def _upsert_target_sql(self) -> str:
        return (
            "INSERT INTO target (newsroom_url, domain, a_id) VALUES (?, ?, ?) "
            "ON CONFLICT(newsroom_url) DO UPDATE SET "
            "  domain = excluded.domain, a_id = excluded.a_id"
        )

    def _select_due_sql(self, limit: int) -> str:
        return (
            "SELECT domain FROM domain_state "
            "WHERE shard BETWEEN ? AND ? AND enabled = 1 "
            "  AND next_allowed_at <= ? AND leased_until < ? "
            "ORDER BY next_allowed_at LIMIT " + str(int(limit))
        )

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# MySQL - production
# ---------------------------------------------------------------------------

_MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS domain_state (
  domain            VARCHAR(255) NOT NULL,
  a_id              INT NOT NULL,
  newsroom_url      TEXT NOT NULL,
  shard             SMALLINT NOT NULL,
  enabled           TINYINT(1) NOT NULL DEFAULT 1,
  next_allowed_at   DATETIME NOT NULL,
  leased_until      DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00',
  lease_owner       VARCHAR(64) NULL,
  crawl_delay_s     FLOAT NOT NULL DEFAULT 5.0,
  revisit_period_s  INT NOT NULL DEFAULT 86400,
  consec_failures   SMALLINT NOT NULL DEFAULT 0,
  discovery_method  VARCHAR(32) NULL,
  feed_url          TEXT NULL,
  etag              VARCHAR(255) NULL,
  last_modified     VARCHAR(255) NULL,
  needs_browser     TINYINT(1) NOT NULL DEFAULT 0,
  needs_browser_at  DATETIME NULL,
  last_success_at   DATETIME NULL,
  p50_body_len      INT NULL,
  PRIMARY KEY (domain),
  KEY idx_lease (shard, next_allowed_at, enabled, leased_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_MYSQL_TARGET_DDL = """
CREATE TABLE IF NOT EXISTS target (
  newsroom_url      VARCHAR(768) NOT NULL,
  domain            VARCHAR(255) NOT NULL,
  a_id              INT NOT NULL,
  enabled           TINYINT(1) NOT NULL DEFAULT 1,
  discovery_method  VARCHAR(32) NULL,
  feed_url          TEXT NULL,
  etag              VARCHAR(255) NULL,
  last_modified     VARCHAR(255) NULL,
  last_success_at   DATETIME NULL,
  consec_failures   SMALLINT NOT NULL DEFAULT 0,
  p50_body_len      INT NULL,
  feed_absent       TINYINT(1) NOT NULL DEFAULT 0,
  probed_at         DATETIME NULL,
  PRIMARY KEY (newsroom_url),
  KEY idx_target_domain (domain, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class MySQLFrontier(Frontier):
    """Requires MySQL 8.0.1+ for SKIP LOCKED."""

    placeholder = "%s"

    def __init__(self, **connect_kwargs: Any):
        import pymysql  # lazy: SQLite users need no driver installed

        connect_kwargs.setdefault("charset", "utf8mb4")
        connect_kwargs.setdefault("autocommit", True)
        self.conn = pymysql.connect(**connect_kwargs)
        self._depth = 0

    def create_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_MYSQL_DDL)
            cur.execute(_MYSQL_TARGET_DDL)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return list(cur.fetchall() or [])

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(sql, [tuple(r) for r in rows])

    def _transaction(self):
        store = self

        class _Txn:
            def __enter__(self):
                if store._depth == 0:
                    store.conn.begin()
                store._depth += 1

            def __exit__(self, exc_type, *_):
                store._depth -= 1
                if store._depth == 0:
                    if exc_type:
                        store.conn.rollback()
                    else:
                        store.conn.commit()
                return False

        return _Txn()

    def _upsert_sql(self) -> str:
        return (
            "INSERT INTO domain_state (domain, a_id, newsroom_url, shard, next_allowed_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  a_id = VALUES(a_id), newsroom_url = VALUES(newsroom_url), shard = VALUES(shard)"
        )

    def _upsert_target_sql(self) -> str:
        return (
            "INSERT INTO target (newsroom_url, domain, a_id) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE domain = VALUES(domain), a_id = VALUES(a_id)"
        )

    def _select_due_sql(self, limit: int) -> str:
        # SKIP LOCKED is what lets concurrent workers claim disjoint domains
        # without blocking each other. Without it, workers serialize on the
        # same head-of-queue rows.
        return (
            "SELECT domain FROM domain_state "
            "WHERE shard BETWEEN %s AND %s AND enabled = 1 "
            "  AND next_allowed_at <= %s AND leased_until < %s "
            "ORDER BY next_allowed_at LIMIT " + str(int(limit)) + " "
            "FOR UPDATE SKIP LOCKED"
        )

    def close(self) -> None:
        self.conn.close()


def open_frontier() -> Frontier:
    """Pick a backend from the environment.

    `SCRAPEV3_FRONTIER` decides when set (`sqlite` or `mysql`); otherwise the
    presence of a MySQL host does. The explicit override exists because the
    frontier and the output sink now share one set of connection settings:
    configuring MySQL for `tns.press_release` should not silently relocate a
    frontier that has months of per-domain state - learned feed URLs,
    discovery methods, failure counts - in a SQLite file.
    """
    backend = os.environ.get("SCRAPEV3_FRONTIER", "").strip().lower()
    host = os.environ.get("SCRAPEV3_MYSQL_HOST", "").strip()
    if backend == "sqlite":
        host = ""
    elif backend == "mysql" and not host:
        raise RuntimeError(
            "SCRAPEV3_FRONTIER=mysql but SCRAPEV3_MYSQL_HOST is not set")
    if host:
        store: Frontier = MySQLFrontier(
            host=host,
            port=int(os.environ.get("SCRAPEV3_MYSQL_PORT", "3306")),
            user=os.environ.get("SCRAPEV3_MYSQL_USER", ""),
            password=os.environ.get("SCRAPEV3_MYSQL_PASSWORD", ""),
            database=os.environ.get("SCRAPEV3_MYSQL_STATE_DB", "scrapev3"),
        )
    else:
        data_dir = os.environ.get("SCRAPEV3_DATA_DIR", "./data")
        store = SQLiteFrontier(Path(data_dir) / "frontier.sqlite")

    # `SCRAPEV3_SCHEDULE=off` makes every enabled domain permanently due. See
    # `Frontier.schedule_enabled` for what that does and does not relax.
    store.schedule_enabled = (
        os.environ.get("SCRAPEV3_SCHEDULE", "on").strip().lower()
        not in {"off", "0", "false", "no"})
    store.create_schema()
    return store
