# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A layout-agnostic news / press-release scraper for 5k–50k institutional sites
(`.edu`, `.gov`, `.org`), replacing a predecessor ("v2") whose per-site CSS
selectors caused 77% of its extraction failures. **The core constraint: no
hand-written per-site selectors, and no per-site knowledge in global code.** If a
fix would encode "on site X, do Y", it belongs in per-domain data (Phase 5/6),
not in the cascade. Everything is open-source and self-hosted — no LLM API, no
proxies, no cloud.

`README.md` is the design record: measured survey numbers, every silent-failure
bug found in live runs and why each fix is shaped the way it is, and the
`tns.press_release` contract. Read the relevant section before changing
discovery, extraction, or the TNS row.

## Commands

```bash
# setup (Windows paths; use .venv/bin/... on WSL2/Linux)
.venv/Scripts/pip install -e .[sink,dev]
cp .env.example .env

.venv/Scripts/python -m pytest tests/ -q              # full suite, ~15s
.venv/Scripts/python -m pytest tests/test_urls.py -q  # one file
.venv/Scripts/python -m pytest tests/test_extract.py::test_name -q
.venv/Scripts/python -m pytest tests/ -q -k jitter    # by keyword

scrapev3 doctor                          # deps, identity, MySQL, tns tables
scrapev3 seed data/sites.csv             # load targets into the frontier
scrapev3 frontier                        # what is due, and which schedule mode
scrapev3 crawl --domains 10 --max-articles 5
scrapev3 crawl --domain example.org --debug    # log every decision, one line each
scrapev3 show --full --limit 3           # what the extractor actually got
scrapev3 audit --limit 60                # discovery correctness, read-only
scrapev3 audit --rescore data/audits/x.jsonl   # re-score saved evidence, no fetching
scrapev3 tns status | tns backfill [--resync] | tns show --full
scrapev3 reset --a-id 22385 --tns        # forget one agency across all stores
scrapev3 remove --a-id 22385             # honour a removal request, permanently
scrapev3 remove --list | remove --apply  # the shared list; reconcile it now
scrapev3 status --health empty           # per-agency health; --publish for the website
scrapev3 status --json clients/sample_status.json   # refresh the demo page's fixture

python scripts/extract_sites.py          # scrape_test.csv -> data/sites.{csv,txt}
python scripts/validate_targets.py run --limit 100   # end-to-end stored-article sweep
python scripts/sql.py "SELECT * FROM domain_state LIMIT 5"   # read-only, safe mid-crawl
```

Tests are offline except `tests/test_politeness.py`, which spins up a local HTTP
server and asserts against real request arrival/completion timestamps.

**`crawl` is deliberately slow** — 5s/host, concurrency 1 per host, so ~10 domains
takes minutes. `--concurrency` raises parallel *domains* only; per-host pacing is
never negotiable.

## Architecture

Pipeline per target: **discover → dedup check → fetch → extract → store**. The
dedup check sits *before* the article fetch, so an already-seen article costs
nothing on a daily re-crawl.

| Module | Role |
|---|---|
| [frontier/store.py](src/scrapev3/frontier/store.py) | Domain-lease queue. SQLite (default) and MySQL backends behind one ABC |
| [fetch/client.py](src/scrapev3/fetch/client.py) | `PoliteFetcher` — pacing, robots, conditional GET, bot-wall detection |
| [discover/sources.py](src/scrapev3/discover/sources.py) | The discovery cascade and its corroboration rules |
| [extract/cascade.py](src/scrapev3/extract/cascade.py) | Per-field extraction cascade; `body.py`/`dates.py`/`metadata.py` are its rungs |
| [crawl.py](src/scrapev3/crawl.py) | `crawl_once` — leases domains, drives targets, applies the crawl-level vetoes |
| [sink.py](src/scrapev3/sink.py) | JSONL archive + SQLite dedup index + `tns_state` |
| [tns/](src/scrapev3/tns/) | `record.py` composes the row (pure, no I/O), `agencies.py` reads lookups, `sink.py` inserts |
| [urls.py](src/scrapev3/urls.py) | `registrable_domain`, `canonical_url`, `classify_url`, `is_non_news_path` |
| [audit.py](src/scrapev3/audit.py) | Scores discovery output for plausibility without fetching articles |
| [removal.py](src/scrapev3/removal.py) | Purges one `a_id` from every store; owns the shared removal list |
| [status.py](src/scrapev3/status.py) | Per-agency health for the website's grid; `classify` is pure |
| [tracing.py](src/scrapev3/tracing.py) | `--debug` decision logging, printed through the progress bar's Console |

**The queue hands out domain leases, not URLs.** That is the load-bearing
decision: one worker owns an eTLD+1 at a time, so politeness is mutual exclusion
enforced locally rather than a distributed rate limiter that fails open. All of a
domain's targets are crawled under the one lease (`house.gov` carries 417).

**`registrable_domain` (eTLD+1, via the public suffix list) is both the pacing key
and the shard key.** Getting it wrong either hammers a publisher or splits one
publisher across two workers — neither fails loudly.

**Discovery order is `cms_api → rss → sitemap → listing`,** ordered by measured
yield rather than by the literature. Whatever a source supplies for free
(headline, date, full body) is passed to the extractor as publisher-asserted
truth, so the cheap sources are also the accurate ones. A solved domain takes a
fast path straight back to its cached source.

**Extraction resolves fields independently** — headline usually from JSON-LD/OG,
body almost always from trafilatura (`articleBody` appears on 1.6% of pages, not
the "68%" the literature claims). Every field records its `Path` (provenance);
this drives drift detection, so **never credit a body to the wrong source** —
feed/wp-json bodies are `FEED`/`CMS_API`, not `TRAFILATURA`.

## Invariants worth knowing before editing

- **A site-wide source must corroborate before it wins.** `/rss.xml`,
  `/wp-json/wp/v2/posts` and an unscoped sitemap all answer for the whole site.
  Each must overlap with links on the target's own page, and at least 50% of what
  it returns must be this publisher's own domain (`usable()` in
  `discover/sources.py`). Three sites needed three separate patches before this
  was made general.
- **"Found articles" is not success.** A source returning only the listing page
  itself, or another publisher's content, must be rejected *inside* discovery —
  otherwise it caches as the winning method and the cascade never retries.
- **The discovery cache is as sticky for a wrong answer as a right one.** `reset`
  deliberately keeps the cached source; `reset --relearn` forces a cold cascade
  (slow — nine feed probes plus a sitemap walk at the per-host delay).
- **Three stores must move together** when re-testing a site: `tns.press_release`,
  `data/articles.sqlite` (dedup — `seen_url` runs before the fetch, so skipping it
  makes a re-run silently store nothing), and the frontier calendar. `reset --tns`
  does all three. Don't reach for `crawl --refetch` while the MySQL rows still exist.
- **`reset` and `remove` are different operations.** `reset` un-does a test run;
  the agency stays in the frontier and comes back on the next crawl. `remove`
  honours a publisher's request: it purges the `a_id` everywhere *and* records it
  on a shared MySQL list (`removed_agency`) that `seed` consults, because `seed`
  upserts every row of `data/sites.csv` and would otherwise resurrect it. The
  list is reconciled — the whole set is re-applied each pass, never drained —
  so two crawlers cannot each consume the other's removals. The website inserts
  into that one table; see [clients/](clients/). Off unless `SCRAPEV3_REMOVAL=on`.
- **The crawler decides health; the website only draws it.** `agency_status`
  carries a `health` word, a `severity` (`ok`/`warn`/`error`) and a `reason`
  sentence, so `clients/status.php` renders without re-deriving anything — two
  definitions of "healthy" would drift and the grid would quietly disagree with
  the crawler. `severity` is a closed vocabulary and unknown words resolve to
  `warn`, so a new fault added here shows as a problem on a website nobody has
  redeployed. Two distinctions carry the weight: `quiet` (we crawl it fine, the
  publisher has not posted) is *not* a fault, and `empty` (we crawl it fine and
  store nothing) is a different fault from `stale` (we stopped reaching it) —
  `empty` was 92 of the first 324 agencies crawled. Off unless `SCRAPEV3_STATUS=on`.
- **Discovery can change which domain a target belongs to.** A publisher that
  redirects to a new home (`dni.gov` -> `odni.gov`) has its identity re-derived
  mid-cascade, so downstream code must read `Discovery.target_domain` rather than
  the domain it started with — otherwise every article it found is discarded as
  off-domain. Relative hrefs resolve against `resp.final_url` for the same reason.
- **Structural signals over class names.** The listing-page fallback filters by
  `<nav>/<header>/<footer>/<aside>` landmarks, because guessing class names is
  exactly the rot this project exists to eliminate.
- **`press_release` is latin1.** Everything goes through `unidecode`; a curly
  quote is not untidy, it is unstorable. The filename, body template and column
  list are v2's contract and must not drift — `tests/test_tns.py` pins them
  character by character, including the three deliberate departures.
- **Dedup is canonical-URL + content hash**, never v2's synthesized filename
  (which collided silently on same-day articles). A genuine filename collision
  widens the filename instead of dropping the document.
- Politeness invariants (concurrency 1/host, ±30% jitter, `Crawl-delay` as a
  floor, per-IP cap, latency-adaptive backoff even on HTTP 200) are proven by
  `tests/test_politeness.py`, not left to convention. Don't weaken them for speed.

## Conventions

- **All config comes from `.env`** via `settings.py` (plain dataclasses, one
  source — no pydantic, no yaml). Both predecessor repos committed live
  credentials; `.gitignore` here is deliberately broad.
- `SCRAPEV3_SCHEDULE=off` in the prototype: every enabled domain is permanently
  due. The *lease* is always on; only the calendar is disabled.
- Windows: the CLI forces the selector event loop (curl_cffi needs `add_reader`)
  and UTF-8 console output. Production should run under WSL2.
- `pytest` runs with `asyncio_mode = "auto"` — async tests need no decorator.
- Comments here explain *why*, usually naming the real site and the measured
  number behind a rule (`battelle.org`, `fightcancer.org`, `ufw.org`). Match that
  when adding a guard: a rule without its evidence gets tuned away later.
- Decision logging uses lazy `%s` interpolation, never f-strings: it sits in the
  per-article loop and must cost nothing when `--debug` is off.
- Every silent-quality bug fixed gets a regression test. That is what the suite is
  for — the failures that matter are plausible-looking wrong data, not exceptions.
- `dbs/` holds restores of the live `tns` schema and is gitignored.
  **`dbs/press_releases.sql` ends with `drop table` / `truncate press_release`** —
  running the whole file empties the live table.
