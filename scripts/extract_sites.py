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
    ap.add_argument("--strict", action="store_true",
                    help="Exit non-zero when the source file has defects. For a "
                         "pipeline that should refuse to proceed on dirty input; "
                         "the report prints either way.")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        return 2

    rows = list(csv.reader(src.open(encoding="utf-8-sig", errors="replace")))
    data = [r for r in rows if r and not r[0].lstrip().startswith("#")]

    # The header declares the field count the rest of the file must match. A row
    # with an extra field means a stray comma, and every column after it is
    # shifted by one - which is how v2 silently broke two sites. Counting these
    # as "skipped" hid them; they are named now.
    header = rows[0] if rows else []
    expected_fields = len(header) if header else None

    seen: set[str] = set()
    seen_ids: dict[str, str] = {}
    out: list[tuple[str, str, str]] = []
    problems: dict[str, list[str]] = {
        "wrong field count": [],
        "duplicate a_id": [],
        "duplicate url": [],
        "unusable url": [],
        "non-numeric a_id": [],
    }

    for r in data:
        if len(r) < 2:
            problems["wrong field count"].append(f"{r[0] if r else '?'}: {len(r)} fields")
            continue
        a_id = r[0].strip()

        if expected_fields and len(r) != expected_fields:
            problems["wrong field count"].append(
                f"a_id {a_id}: {len(r)} fields, header declares {expected_fields}")
            # Field 1 is still readable - the shift is after it - so the row is
            # kept. Reporting it is what matters; a stray comma one field
            # earlier would give this a_id somebody else's URL.

        if not a_id.isdigit():
            problems["non-numeric a_id"].append(repr(a_id))

        # Field 1 is "url" or "url|base_domain"; the base was only used by v2
        # to resolve relative hrefs, which v3 does with urljoin.
        raw = r[1].split("|")[0].strip()
        # The seed is a URL we FETCH, so it keeps the host the publisher
        # actually uses. `canonical_url` strips a decorative `www.`, which is
        # right for a dedup key and wrong here: a site that serves only on
        # `www.` would be seeded with a host that may not resolve. Canonical
        # form is used for the duplicate check only.
        url = raw if raw.lower().startswith(("http://", "https://")) else canonical_url(raw)
        key = canonical_url(raw)
        dom = registrable_domain(key)
        if not key or not dom:
            problems["unusable url"].append(f"a_id {a_id}: {r[1][:60]!r}")
            continue
        if key in seen:
            problems["duplicate url"].append(f"a_id {a_id}: {key[:60]}")
            continue
        if a_id in seen_ids:
            # Not fatal - one agency can legitimately have several newsroom
            # URLs - but it means `--a-id` will crawl more than one target, so
            # it should be visible rather than discovered later.
            problems["duplicate a_id"].append(f"a_id {a_id}: also {url[:52]}")
        seen.add(key)
        seen_ids.setdefault(a_id, key)
        out.append((a_id, url, dom))

    skipped = sum(len(v) for k, v in problems.items()
                  if k in ("wrong field count", "unusable url", "duplicate url"))

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

    found = {k: v for k, v in problems.items() if v}
    if found:
        print("\nPROBLEMS IN THE SOURCE FILE")
        for kind, items in found.items():
            print(f"  {kind} ({len(items)}):")
            for item in items[:6]:
                print(f"      {item}")
            if len(items) > 6:
                print(f"      ... and {len(items) - 6} more")
        print("\nThese are data defects, not code defects - fix them in the source"
              "\nCSV or in tns.agencies. A stray comma one field earlier than the"
              "\nones above would give an agency somebody else's URL, which for a"
              "\nnewswire is an attribution error rather than a cosmetic one.")
        # Exit 0 by default: the site list above was produced correctly, and
        # failing a command that succeeded is its own kind of lie. --strict is
        # for a pipeline that should refuse to proceed on dirty input.
        if args.strict:
            print("\n--strict: exiting non-zero because the source file has defects.")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
