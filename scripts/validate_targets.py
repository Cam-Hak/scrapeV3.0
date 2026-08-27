"""Validate stored articles against the newsroom page they claim to come from.

The audit measures what *discovery* returns. This measures what actually got
**stored**: crawl a target for real, then ask whether the articles now in the
sink are the ones the publisher lists on the page we were pointed at.

The distinction matters because every defect found in the first sweep survived
the audit. `centerforfoodsafety.org` scored a perfect overlap and `usable:
True` while storing every headline as
`Center for Food Safety | Press Releases |  | <headline>`. Overlap cannot see a
corrupted headline, because overlap only looks at URLs.

Two further gaps this closes:

* The audit keys on **domain** - 1,747 rows for 2,401 targets. 80 domains hold
  more than one target, so 654 targets are assessed by nothing at all.
  `house.gov` alone collapses 417 targets into one row.
* The audit never fetches a stored article, so it cannot check headline text,
  date agreement, or whether the article's host is even the target's host.

Work order is a **seeded shuffle**, held in the ledger. Any prefix of it is
therefore a random sample of the corpus, so partial progress still supports a
corpus-wide estimate - and the order never changes between runs, so the sweep
resumes exactly where it stopped.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrapev3.urls import canonical_url, registrable_domain   # noqa: E402

PROJ = Path(__file__).resolve().parent.parent
LEDGER = PROJ / "data" / "validation" / "ledger.sqlite"
WORK = PROJ / "data" / "validation" / "work"
SHUFFLE_SEED = 20260827

UA = "TNSNewsBot/1.0 (+https://targetednews.com/bot)"

# A stored article set with fewer than this share of its URLs on the newsroom
# page is looking somewhere the publisher did not point us.
LOW_OVERLAP = 0.60
# Below this many *articles* on the newsroom page there is nothing to
# corroborate against, and the overlap figure is noise rather than evidence.
# Counting raw links instead of articles was the first version's mistake: nav
# and footer chrome clears any link threshold on a page listing no articles.
MIN_CONTENT_LINKS = 5
# Headlines sharing a prefix this long across a target are carrying site-name
# boilerplate from <title>/og:title rather than a real common phrase.
BOILERPLATE_PREFIX = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS target (
  a_id          INTEGER NOT NULL,
  newsroom_url  TEXT NOT NULL,
  domain        TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  state         TEXT NOT NULL DEFAULT 'pending',
  verdict       TEXT,
  method        TEXT,
  n_discovered  INTEGER,
  n_stored      INTEGER,
  overlap       REAL,
  page_links    INTEGER,
  codes         TEXT,
  detail        TEXT,
  tested_at     TEXT,
  source        TEXT,
  PRIMARY KEY (a_id, newsroom_url)
);
CREATE INDEX IF NOT EXISTS target_seq  ON target(seq);
CREATE INDEX IF NOT EXISTS target_state ON target(state);

-- Evidence is kept so a verdict can be re-scored without crawling again. The
-- first sweep had to be thrown away and re-run because it kept only the score,
-- and a scoring bug then cost a full re-crawl.
CREATE TABLE IF NOT EXISTS evidence (
  a_id          INTEGER NOT NULL,
  newsroom_url  TEXT NOT NULL,
  stored        TEXT,   -- JSON: url, headline, published_at, body_len per article
  content_links TEXT,   -- JSON: the newsroom page's own article set (chrome removed)
  n_content     INTEGER,
  captured_at   TEXT,
  PRIMARY KEY (a_id, newsroom_url)
);
"""


def db() -> sqlite3.Connection:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER, isolation_level=None, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------

def cmd_seed(args: argparse.Namespace) -> int:
    """Load every target from the frontier and fix the work order."""
    front = sqlite3.connect(PROJ / "data" / "frontier.sqlite")
    rows = front.execute(
        "SELECT a_id, newsroom_url, domain FROM target ORDER BY a_id, newsroom_url"
    ).fetchall()

    order = list(range(len(rows)))
    random.Random(SHUFFLE_SEED).shuffle(order)

    conn = db()
    conn.execute("BEGIN")
    for seq, (a_id, url, domain) in zip(order, rows):
        conn.execute(
            "INSERT INTO target (a_id, newsroom_url, domain, seq) VALUES (?,?,?,?) "
            "ON CONFLICT(a_id, newsroom_url) DO NOTHING",
            (a_id, url, domain, seq),
        )
    conn.execute("COMMIT")

    n = conn.execute("SELECT COUNT(*) FROM target").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM target WHERE state='done'").fetchone()[0]
    print(f"ledger holds {n} targets ({done} already recorded), work order seeded {SHUFFLE_SEED}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Write a hand-adjudicated verdict in, so manual work is never redone."""
    conn = db()
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    for row in payload:
        conn.execute(
            "UPDATE target SET state='done', verdict=?, codes=?, detail=?, "
            "tested_at=?, source=? WHERE a_id=?",
            (row["verdict"], ",".join(row.get("codes", [])), row.get("detail"),
             row.get("tested_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
             row.get("source", "manual"), row["a_id"]),
        )
    print(f"recorded {len(payload)} adjudicated targets")
    return 0


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def fetch(url: str, timeout: int = 30) -> str:
    """Plain fetch with the crawler's own identity. Empty string on failure."""
    try:
        out = subprocess.run(
            ["curl", "-sS", "-L", "--compressed", "-A", UA,
             "--max-time", str(timeout), url],
            capture_output=True, timeout=timeout + 10,
        )
        return out.stdout.decode("utf-8", errors="replace")
    except Exception:                                        # noqa: BLE001
        return ""


def page_links(html: str, base: str) -> set[str]:
    """Every internal link on the newsroom page, canonicalised for comparison."""
    out: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\'#]+)["\']', html, re.I):
        href = m.group(1).strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        try:
            full = canonical_url(href, base)
        except Exception:                                    # noqa: BLE001
            continue
        if full:
            out.add(full.rstrip("/").lower())
    return out


def url_key(url: str, base: str | None = None) -> str:
    """A comparison key that survives the cosmetic differences between how a
    listing links to an article and how the crawler stored it.

    `castor.house.gov` serves `documentsingle.aspx?DocumentID=405270` from its
    feed and links `documentsingle.aspx?documentid=405232` from its listing —
    same application, different scheme and different query-key casing. Compared
    raw, every such target scores zero overlap and looks broken.
    """
    try:
        u = canonical_url(url, base) if base else url
    except Exception:                                        # noqa: BLE001
        u = url
    s = urlsplit(u)
    host = s.netloc.lower().removeprefix("www.").split(":")[0]
    path = s.path.rstrip("/").lower()
    query = "&".join(sorted(p.lower() for p in s.query.split("&") if p))
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def slug(url: str) -> str:
    """The last meaningful path segment — an article's identity across aliases.

    `kelly.senate.gov` links a release from its listing as
    `/newsroom/press-releases/icymi-kelly-...` and serves the same article from
    its feed as `/icymi-kelly-...`. Matching on the full path calls that a miss
    and the target looks broken; matching on the slug recognises it.
    """
    segs = [s for s in urlsplit(url).path.strip("/").split("/") if s]
    return segs[-1].lower() if segs else ""


def section_prefix(urls: list[str]) -> str:
    """The path section a set of article URLs sits under, e.g. `/news-central`.

    Articles published at the root (`/some-headline-slug`) have no section, so
    the first segment is the slug itself and would differ for every article.
    Those collapse to "/" — a shared root is still a shared section.
    """
    counts: dict[str, int] = {}
    for u in urls:
        segs = [s for s in urlsplit(u).path.strip("/").split("/") if s]
        key = "/" + segs[0] if len(segs) > 1 else "/"
        counts[key] = counts.get(key, 0) + 1
    return max(counts, key=counts.get) if counts else ""


_home_cache: dict[str, set[str]] = {}
_home_lock = threading.Lock()


def homepage_links(url: str) -> set[str]:
    """Links on the site's homepage — i.e. its chrome. Cached per host.

    house.gov alone holds 417 targets, so without the cache this would refetch
    the same homepage hundreds of times.
    """
    s = urlsplit(url)
    key = s.netloc.lower()
    with _home_lock:
        if key in _home_cache:
            return _home_cache[key]
    home = f"{s.scheme}://{s.netloc}/"
    links = page_links(fetch(home), home)
    with _home_lock:
        _home_cache[key] = links
    return links


def content_links(html: str, url: str) -> set[str]:
    """The newsroom page's *own* article set, with site chrome subtracted.

    A newsroom page is the publisher's statement of what belongs in that
    section, but a raw link scrape is mostly nav and footer — enough to clear
    any naive link-count threshold while containing no articles at all. What
    survives subtracting the homepage's links, restricted to this host and to
    slugs long enough to be a headline, is the section's real contents.

    This is what makes a zero-overlap verdict trustworthy: an empty result here
    means the page listed nothing we could compare against (client-rendered,
    or behind a query form), which is `unverifiable` — not `broken`.
    """
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    uniq = page_links(html, url) - homepage_links(url)
    return {u for u in uniq
            if host in u and len(urlsplit(u).path.rstrip("/").split("/")[-1]) > 18}


def common_prefix(strings: list[str]) -> str:
    if len(strings) < 2:
        return ""
    first, last = min(strings), max(strings)
    i = 0
    while i < min(len(first), len(last)) and first[i] == last[i]:
        i += 1
    return first[:i]


# A shared headline prefix only means boilerplate when it carries the kind of
# separator a page title uses between site name and headline. A bare shared
# phrase is not enough - a congressmember's releases all legitimately begin
# "Rep. Vasquez", and flagging those would bury the real cases.
TITLE_SEP = re.compile(r"\||\s[–—»›·-]\s")

GENERIC_TITLES = re.compile(
    r"^(news|news and events|newsroom|press|press releases?|media|"
    r"media centre|media center|announcements|latest news|blog)$", re.I)


def score(url: str, stored: list[dict], content: set[str]) -> tuple[str, list[str], str]:
    """Score one target from persisted evidence. No network access.

    Returns (verdict, codes, human-readable detail).
    """
    codes: list[str] = []
    notes: list[str] = []
    target_host = urlsplit(url).netloc.lower().removeprefix("www.")

    evidential = len(content) >= MIN_CONTENT_LINKS

    if not stored:
        if not evidential:
            return ("unverifiable", ["zero_yield", "no_listing"],
                    f"crawl stored nothing, and the newsroom page exposed only "
                    f"{len(content)} article(s) to compare against")
        return ("broken", ["zero_yield"],
                f"crawl stored no articles while the newsroom page lists {len(content)}")

    if not evidential:
        codes.append("no_listing")
        notes.append(f"newsroom page exposed only {len(content)} article link(s) to a "
                     f"plain fetch; overlap is not evidence either way")
        overlap = None
    else:
        keys = {url_key(c) for c in content}
        slugs = {slug(c) for c in content if slug(c)}
        hit = sum(1 for a in stored
                  if url_key(a["url"], url) in keys or slug(a["url"]) in slugs)
        overlap = hit / len(stored)
        if overlap < LOW_OVERLAP:
            # Same section but different items is the listing being paginated or
            # stale in plain HTML, not discovery looking somewhere else. Only a
            # different section is evidence of the latter.
            want = section_prefix(sorted(content))
            # Use the same collapsing rule on both sides. Computing the stored
            # side inline meant a root-published article's "section" was its own
            # slug, so a site whose articles live at the root could never match
            # its own listing and was always called broken.
            same_section = sum(1 for a in stored
                               if section_prefix([a["url"]]) == want)
            if same_section >= 0.8 * len(stored):
                codes.append("selection_order")
                notes.append(f"{hit}/{len(stored)} stored URLs are on the page, but all "
                             f"sit under {want} like the listing — different items, "
                             f"right section")
            else:
                codes.append("low_overlap")
                notes.append(f"{hit}/{len(stored)} stored URLs are on the newsroom page "
                             f"(which lists {len(content)}); stored sit under "
                             f"{section_prefix([a['url'] for a in stored])}, listing under {want}")

    # D4 - the publisher guard compares eTLD+1, so a sibling subdomain passes.
    off_host = [a for a in stored
                if urlsplit(a["url"]).netloc.lower().removeprefix("www.") != target_host]
    if off_host:
        codes.append("off_host")
        hosts = sorted({urlsplit(a["url"]).netloc for a in off_host})
        notes.append(f"{len(off_host)} article(s) from another host: {', '.join(hosts[:3])}")

    # D3 - site-name boilerplate copied out of <title>/og:title. The tell is a
    # shared prefix ending in a title *separator*, not merely a shared prefix:
    # a congressmember's releases all legitimately start "Rep. Vasquez", and
    # flagging those would bury the real cases.
    heads = [a["headline"] or "" for a in stored]
    pre = common_prefix(heads)
    if len(stored) > 1 and len(pre) >= BOILERPLATE_PREFIX and TITLE_SEP.search(pre):
        codes.append("headline_boilerplate")
        notes.append(f"every headline starts {pre.strip()!r}")

    # D5 - a section index page stored as though it were an article.
    for a in stored:
        if GENERIC_TITLES.match((a["headline"] or "").strip()):
            codes.append("index_page")
            notes.append(f"stored a section index titled {a['headline']!r}")
            break

    undated = [a for a in stored if not a.get("published_at")]
    if undated:
        codes.append("undated")
        notes.append(f"{len(undated)} article(s) stored with no publish date")

    thin = [a for a in stored if (a.get("quality") or {}).get("body_len", 0) < 500]
    if thin:
        codes.append("thin_body")
        notes.append(f"{len(thin)} article(s) under 500 chars")

    # D2 - stored set trails the page. Only meaningful when the page dates parse.
    dates = sorted(a["published_at"] for a in stored if a.get("published_at"))
    if dates and overlap is not None:
        span_days = (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).days
        if span_days > 120:
            codes.append("stale_spread")
            notes.append(f"stored dates span {span_days}d - selection is not recency-ordered")

    hard = {"off_host", "index_page", "headline_boilerplate", "low_overlap"}
    if hard & set(codes):
        verdict = "broken"
    elif "no_listing" in codes:
        # Nothing to corroborate against. Say so rather than guessing either way.
        verdict = "unverifiable"
    elif codes:
        verdict = "drift"
    else:
        verdict = "ok"
    return verdict, codes, "; ".join(notes)


# --------------------------------------------------------------------------
# running one target
# --------------------------------------------------------------------------

def run_target(row: dict, cap: int, window: int) -> dict:
    """Crawl one target in isolation, read its newsroom page, score the pair."""
    a_id = row["a_id"]
    d = WORK / str(a_id)
    shutil.rmtree(d, ignore_errors=True)
    (d / "articles").mkdir(parents=True, exist_ok=True)
    shutil.copy(PROJ / "data" / "frontier.sqlite", d / "frontier.sqlite")

    env = dict(os.environ, SCRAPEV3_DATA_DIR=str(d), SCRAPEV3_SINK="jsonl")
    log = ""
    try:
        proc = subprocess.run(
            [str(PROJ / ".venv" / "Scripts" / "scrapev3.exe"), "crawl",
             "--a-id", str(a_id), "--max-articles", str(cap),
             "--max-age-days", str(window)],
            cwd=PROJ, env=env, capture_output=True, timeout=900,
        )
        log = proc.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log = "(timed out)"

    stored = []
    for f in glob.glob(str(d / "articles" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            try:
                stored.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    m = re.search(r"Articles discovered[^│]*│\s*(\d+)", log)
    n_disc = int(m.group(1)) if m else None
    mm = re.findall(r"│\s*(rss|sitemap|cms_api|listing|news_sitemap|none)\s*│\s*\d+", log)
    method = mm[0] if mm else None

    url = row["newsroom_url"]
    html = fetch(url)
    content = content_links(html, url) if html else set()

    if not html:
        v, codes, detail = ("unreachable", ["newsroom_unfetchable"],
                            "newsroom page could not be fetched for comparison")
    else:
        v, codes, detail = score(url, stored, content)

    ov = None
    if stored and content:
        keys = {url_key(c) for c in content}
        slugs = {slug(c) for c in content if slug(c)}
        hit = sum(1 for a in stored
                  if url_key(a["url"], url) in keys or slug(a["url"]) in slugs)
        ov = round(hit / len(stored), 3)

    keep = [dict(url=a["url"], headline=a.get("headline"),
                 published_at=a.get("published_at"),
                 body_len=(a.get("quality") or {}).get("body_len"))
            for a in stored]

    shutil.rmtree(d, ignore_errors=True)
    return dict(verdict=v, codes=codes, detail=detail, a_id=a_id, newsroom_url=url,
                method=method, n_discovered=n_disc, n_stored=len(stored), overlap=ov,
                page_links=len(content), stored=keep, content=sorted(content))


def cmd_run(args: argparse.Namespace) -> int:
    conn = db()
    rows = [dict(a_id=r[0], newsroom_url=r[1], domain=r[2]) for r in conn.execute(
        "SELECT a_id, newsroom_url, domain FROM target WHERE state='pending' "
        "ORDER BY seq LIMIT ?", (args.limit,))]
    if not rows:
        print("nothing pending")
        return 0

    print(f"validating {len(rows)} targets, {args.concurrency} at a time "
          f"(cap {args.articles}/target, window {args.window}d)")

    # Never put two workers on one registrable domain at once - per-host pacing
    # lives inside a single crawl process and cannot see its siblings.
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(registrable_domain(r["newsroom_url"]), []).append(r)
    waves = []
    while buckets:
        wave = []
        for dom in list(buckets):
            wave.append(buckets[dom].pop(0))
            if not buckets[dom]:
                del buckets[dom]
        waves.append(wave)

    done = 0
    t0 = time.time()
    for wave in waves:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = {pool.submit(run_target, r, args.articles, args.window): r for r in wave}
            for fut in as_completed(futs):
                r = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:                     # noqa: BLE001
                    res = dict(a_id=r["a_id"], newsroom_url=r["newsroom_url"],
                               verdict="error", codes=["runner_error"], detail=str(exc)[:300],
                               method=None, n_discovered=None, n_stored=0, overlap=None,
                               page_links=0, stored=[], content=[])
                conn.execute(
                    "INSERT INTO evidence (a_id, newsroom_url, stored, content_links, "
                    "n_content, captured_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(a_id, newsroom_url) DO UPDATE SET stored=excluded.stored, "
                    "content_links=excluded.content_links, n_content=excluded.n_content, "
                    "captured_at=excluded.captured_at",
                    (res["a_id"], res["newsroom_url"], json.dumps(res["stored"]),
                     json.dumps(res["content"]), len(res["content"]),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                conn.execute(
                    "UPDATE target SET state='done', verdict=?, method=?, n_discovered=?, "
                    "n_stored=?, overlap=?, page_links=?, codes=?, detail=?, tested_at=?, "
                    "source='auto' WHERE a_id=? AND newsroom_url=?",
                    (res["verdict"], res["method"], res["n_discovered"], res["n_stored"],
                     res["overlap"], res["page_links"], ",".join(res["codes"]), res["detail"],
                     datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     res["a_id"], res["newsroom_url"]),
                )
                done += 1
                rate = done / max(time.time() - t0, 1) * 60
                print(f"[{done}/{len(rows)}] {res['verdict']:11} "
                      f"{urlsplit(res['newsroom_url']).netloc[:38]:40} "
                      f"stored={res['n_stored']} ovl={res['overlap']} "
                      f"{','.join(res['codes'])[:60]}  ({rate:.1f}/min)", flush=True)
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-apply the current scoring to stored evidence. Fetches nothing.

    A scoring rule will be wrong at some point; this makes correcting it cost
    seconds instead of a re-crawl.
    """
    conn = db()
    rows = conn.execute(
        "SELECT e.a_id, e.newsroom_url, e.stored, e.content_links, t.verdict "
        "FROM evidence e JOIN target t USING (a_id, newsroom_url)").fetchall()
    changed = 0
    for a_id, url, stored_j, content_j, was in rows:
        stored = json.loads(stored_j or "[]")
        content = set(json.loads(content_j or "[]"))
        for a in stored:                       # score() expects the quality shape
            a.setdefault("quality", {"body_len": a.get("body_len") or 0})
        v, codes, detail = score(url, stored, content)
        ov = None
        if stored and content:
            keys = {url_key(c) for c in content}
            slugs = {slug(c) for c in content if slug(c)}
            hit = sum(1 for a in stored
                      if url_key(a["url"], url) in keys or slug(a["url"]) in slugs)
            ov = round(hit / len(stored), 3)
        conn.execute(
            "UPDATE target SET verdict=?, codes=?, detail=?, overlap=?, page_links=? "
            "WHERE a_id=? AND newsroom_url=?",
            (v, ",".join(codes), detail, ov, len(content), a_id, url))
        if v != was:
            changed += 1
            print(f"   {was or '(none)':13} -> {v:13} {urlsplit(url).netloc[:44]}")
    print(f"rescored {len(rows)} targets from stored evidence, {changed} verdicts changed")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = db()
    total, done = conn.execute(
        "SELECT COUNT(*), SUM(state='done') FROM target").fetchone()
    print(f"tested {done or 0} / {total}  ({(done or 0)/total*100:.1f}%)\n")
    print("verdicts:")
    for v, n in conn.execute(
            "SELECT verdict, COUNT(*) FROM target WHERE state='done' "
            "GROUP BY verdict ORDER BY 2 DESC"):
        print(f"   {v or '(none)':14} {n:5}")
    print("\nfindings:")
    tally: dict[str, int] = {}
    for (codes,) in conn.execute(
            "SELECT codes FROM target WHERE state='done' AND codes<>''"):
        for c in codes.split(","):
            tally[c] = tally.get(c, 0) + 1
    for c, n in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"   {c:22} {n:5}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="Load all frontier targets and fix the work order.")
    s.set_defaults(func=cmd_seed)

    r = sub.add_parser("run", help="Validate the next N pending targets.")
    r.add_argument("--limit", type=int, default=100)
    r.add_argument("--concurrency", type=int, default=6)
    r.add_argument("--articles", type=int, default=5)
    r.add_argument("--window", type=int, default=3650)
    r.set_defaults(func=cmd_run)

    rec = sub.add_parser("record", help="Import hand-adjudicated verdicts from JSON.")
    rec.add_argument("file")
    rec.set_defaults(func=cmd_record)

    rs = sub.add_parser("rescore", help="Re-apply scoring to stored evidence, no fetching.")
    rs.set_defaults(func=cmd_rescore)

    st = sub.add_parser("status", help="Progress and verdict breakdown.")
    st.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
