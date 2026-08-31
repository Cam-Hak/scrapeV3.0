"""Re-audit exactly the targets that were unreachable in the full corpus run.

Not a sample: the specific 149 rows whose verdict the DNS and identity work was
supposed to change. Uses `audit_target` directly - the same code path `scrapev3
audit` uses - because the CLI scopes by a_id, domain or a random sample, and a
one-off measurement does not deserve a new flag.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from scrapev3.audit import audit_target, summarize
from scrapev3.fetch import PoliteFetcher
from scrapev3.frontier import open_frontier
from scrapev3.settings import Settings

PRIOR = Path("data/audits/full-corpus.jsonl")
OUT = Path("data/audits/reaudit-unreachable.jsonl")


def prior_unreachable() -> set[int]:
    ids = set()
    for line in PRIOR.open(encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if not r.get("reachable"):
            ids.add(r["a_id"])
    return ids


async def main(concurrency: int = 8) -> int:
    want = prior_unreachable()
    store = open_frontier()
    try:
        # Public API only: the same lookup `_cmd_audit --a-id` performs.
        rows = []
        for a_id in sorted(want):
            for d in store.domains_for(a_id=a_id):
                for t in store.targets_for(d):
                    if t.a_id == a_id:
                        rows.append((t.a_id, t.domain, t.newsroom_url,
                                     t.discovery_method, t.feed_url,
                                     t.feed_absent))
    finally:
        store.close()
    print(f"{len(want)} previously-unreachable a_ids -> {len(rows)} targets", flush=True)

    settings = Settings.load()
    sem = asyncio.Semaphore(concurrency)
    results = []
    done = 0

    async with PoliteFetcher(settings) as fetcher:
        async def one(row):
            nonlocal done
            a_id, domain, url, method, feed, absent = row
            async with sem:
                try:
                    a = await asyncio.wait_for(
                        audit_target(fetcher, a_id=a_id, domain=domain,
                                     newsroom_url=url, known_method=method,
                                     known_feed=feed, feed_absent=absent,
                                     limit=25),
                        timeout=240)
                except Exception as exc:
                    print(f"  ! {url[:60]} {type(exc).__name__}", flush=True)
                    return
                results.append(a)
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(rows)}", flush=True)

        await asyncio.gather(*(one(r) for r in rows))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for a in results:
            fh.write(json.dumps(a.as_dict()) + "\n")
    summary = summarize(results)
    OUT.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2),
                                                encoding="utf-8")
    print("\n=== re-audit summary ===")
    print(json.dumps(summary, indent=2)[:1400])
    print(f"\nwrote {len(results)} rows to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)))
