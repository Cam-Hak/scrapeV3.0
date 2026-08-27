#!/usr/bin/env python
"""Derive the v3 target list from the legacy v2 CSV.

Reads only the agency id and the newsroom URL. The legacy file also carries 18
columns of hand-authored CSS selectors; v3 does not use them - replacing them
is the entire point of the project.

Usage:
    python scripts/extract_sites.py [source.csv] [--sample N] [--seed N]
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from scrapev3.urls import canonical_url, registrable_domain  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default="scrape_test.csv")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--sample", type=int, default=1000,
                    help="Size of the random distinct-domain survey sample")
    ap.add_argument("--seed", type=int, default=20260826)
    args = ap.parse_args()

    src = Path(args.source)
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        return 2

    rows = list(csv.reader(src.open(encoding="utf-8-sig", errors="replace")))
    data = [r for r in rows if r and not r[0].lstrip().startswith("#")]

    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    skipped = 0
    for r in data:
        if len(r) < 2:
            skipped += 1
            continue
        # Field 1 is "url" or "url|base_domain"; the base was only used by v2
        # to resolve relative hrefs, which v3 does with urljoin.
        url = canonical_url(r[1].split("|")[0].strip())
        dom = registrable_domain(url)
        if not url or not dom or url in seen:
            skipped += 1
            continue
        seen.add(url)
        out.append((r[0].strip(), url, dom))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "sites.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["a_id", "newsroom_url", "domain"])
        w.writerows(out)
    (out_dir / "sites.txt").write_text(
        "\n".join(u for _, u, _ in out) + "\n", encoding="utf-8")

    by_domain: dict[str, str] = {}
    for _, url, dom in out:
        by_domain.setdefault(dom, url)

    random.seed(args.seed)
    n = min(args.sample, len(by_domain))
    sample = random.sample(sorted(by_domain), n)
    (out_dir / "survey_sample.txt").write_text(
        f"# Random sample of {n} distinct domains (seed {args.seed})\n"
        + "\n".join(by_domain[d] for d in sample) + "\n",
        encoding="utf-8")

    tlds = Counter(d.rsplit(".", 1)[-1] for _, _, d in out)
    per_domain = Counter(d for _, _, d in out)

    print(f"source rows        : {len(data)}  (skipped {skipped})")
    print(f"newsroom URLs      : {len(out)}  -> {out_dir/'sites.csv'}")
    print(f"distinct domains   : {len(by_domain)}")
    print(f"survey sample      : {n}  -> {out_dir/'survey_sample.txt'}")
    print(f"top TLDs           : {tlds.most_common(8)}")
    print(f"biggest clusters   : {per_domain.most_common(5)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
