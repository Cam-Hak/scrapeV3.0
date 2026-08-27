"""Stable sharding for the domain frontier.

Two rules, both learned the hard way in distributed crawlers:

1. **Never use Python's built-in `hash()`.** It is randomized per process
   (PYTHONHASHSEED), so a domain would land in a different shard on every
   restart and after every deploy. Workers would silently trade domains and
   the "one worker owns this domain" invariant - which is the entire basis of
   our politeness guarantee - would not hold.

2. **Use fixed virtual shards mapped to workers by range, not `hash % N`.**
   With `hash % worker_count`, changing the worker count reshuffles every
   domain. With 1024 fixed shards assigned as ranges, adding a worker moves a
   few ranges and leaves everything else alone.
"""

from __future__ import annotations

import hashlib

# Fixed for the life of the system. Changing this re-shards everything, so it
# is deliberately generous: 1024 shards divides evenly for 1..8 workers and
# leaves headroom far beyond any plausible worker count here.
NUM_SHARDS = 1024


def shard_for(domain: str) -> int:
    """Map a registrable domain to a stable shard in [0, NUM_SHARDS)."""
    digest = hashlib.blake2b(domain.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % NUM_SHARDS


def shard_ranges(worker_count: int) -> list[tuple[int, int]]:
    """Split the shard space into contiguous inclusive ranges, one per worker.

    Remainder shards are spread across the leading workers rather than dumped
    on the last one, so range sizes differ by at most 1.
    """
    if worker_count < 1:
        raise ValueError("worker_count must be >= 1")
    if worker_count > NUM_SHARDS:
        raise ValueError(f"worker_count must be <= {NUM_SHARDS}")

    base, extra = divmod(NUM_SHARDS, worker_count)
    ranges: list[tuple[int, int]] = []
    start = 0
    for i in range(worker_count):
        size = base + (1 if i < extra else 0)
        ranges.append((start, start + size - 1))
        start += size
    return ranges


def range_for_worker(worker_index: int, worker_count: int) -> tuple[int, int]:
    """Inclusive (lo, hi) shard range this worker owns."""
    if not 0 <= worker_index < worker_count:
        raise ValueError(f"worker_index {worker_index} out of range for {worker_count} workers")
    return shard_ranges(worker_count)[worker_index]
