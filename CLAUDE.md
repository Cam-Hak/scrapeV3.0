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
scrapev3 request --a-id 22385 --url https://x.org/news   # honour an add request
scrapev3 request --list | request --apply       # the shared list; reconcile it now
scrapev3 faults                          # what went wrong, ranked; --owner us
scrapev3 faults --kind dns --runs 7      # one kind, across the last 7 runs
scrapev3 status --health empty           # per-agency health; --publish for the website
scrapev3 status --uncached --limit 40    # newsrooms discovery has never solved
scrapev3 status --json data/status.json  # refresh the demo page's fixture
scrapev3 status --html                   # standalone page, no server; data/status.html
php -S localhost:8000 -t clients         # then /status_demo.php - same page, live rows

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
| [status.py](src/scrapev3/status.py) | Per-agency health *and* inventory for the website's grid; `classify` is pure |
| [faults.py](src/scrapev3/faults.py) | Ranks `failure_kind` words by severity x breadth x owner; owns `data/faults.sqlite` |
| [site_requests.py](src/scrapev3/site_requests.py) | Seeds one `a_id`+URL from the shared request list; mirror of `removal.py` |
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
  was made general. **A declared feed is not exempt.** `<link rel="alternate">`
  proves ownership, not scope, and WordPress emits the site-wide `/feed` into
  every page it renders — so the declaration is honoured only while the feed is
  at least as specific as the section it was declared on
  (`_declaration_is_scoped`). 64 of 1,747 audited targets stored the wrong
  documents through that hole. The unscoped-sitemap **reserve** is the
  deliberate exception: corroborating it was tried and reverted, because these
  newsrooms are JS-rendered and overlap is zero whether the sitemap is right or
  wrong — it dropped `nyclu.org`, which was right, as readily as `bny.com`,
  which was wrong. Those belong in per-domain data. See README, *The
  declaration hole*; `tests/test_corroboration.py` pins all three.
- **Discovery applies the crawl's own URL gate, not just the crawl.** A sitemap
  lists every URL the CMS knows — `sanjac.edu`'s offers `/about/news/_nav.ounav`
  and `/about/news/index.php` under the right prefix. Counted as yield they
  satisfy `usable()`, win the cascade and cache as the method, and every crawl
  then gates them away and stores nothing: `empty` by another road. News-sitemap
  entries are exempt, for the reason feeds are.
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
- **`requested_site` is the mirror of `removed_agency`, and a removal outranks
  it.** The website can insert into both, so it can ask for two incompatible
  things. A request whose `a_id` is on the removal list is refused on *every*
  pass and the refusal is counted - without that rule the crawler purges the
  agency, re-seeds it from the request list next pass, and purges it again
  forever, so a publisher who asked to be removed silently stays in the crawl.
  Reconciled, not drained, for the same reason as removals. Keyed on
  `newsroom_url` alone, exactly like `target`: the frontier cannot hold one URL
  under two agencies, and VARCHAR(768) utf8mb4 is already InnoDB's whole 3072-byte
  key limit. There is deliberately no `domain` column - the crawler derives it,
  since that value is its pacing and shard key. Off unless `SCRAPEV3_REQUESTS=on`.
- **`FAILURE_KINDS` and `ACCESS_VERDICTS` are the vocabularies; `severity` and
  `owner` are derived from them and never stored.** So a rule change re-ranks
  every run already recorded - `audit --rescore`'s property - and a stored row
  can never disagree with the classifier that would judge it today. The maps
  live in `fetch/client.py` beside the words; the ranking lives in `faults.py`.
  Unknown kinds fail loud (severity 3, owner `us`) because an unmapped word is
  our omission; `ok`/`not_modified` score 0 because they are known-good rather
  than unknown; an unranked *band* fails quiet, because a list whose top item
  is noise stops being read. `by_failure` is keyed on the kind, never on
  `resp.error` - that string carries the URL, so it used to fragment one cause
  into one row per article.
- **`policy` is weighted zero, and that is the whole point of the owner axis.**
  Robots refusals and bot walls are recorded with every domain attributed and
  never rank. Counting them as defects puts a rule we are correctly obeying at
  the top of the work queue forever; omitting them loses the attribution that
  made "27 targets refused" knowable at all.
- **The website's sort has to be a total order, and has to match the fixture.**
  `clients/status.{php,py}` build every `ORDER BY` through one whitelist:
  nulls last in both directions (MySQL puts them first ascending), `a_id` as an
  unconditional tiebreak (sorting by `health` leaves 2,000 rows tied, and
  without it paging repeats and skips rows), and `severity` by `FIELD()` rank
  rather than alphabetically. `sort_rows` reproduces it for a site reading the
  JSON fixture and **folds case**, because the table is `utf8mb4_0900_ai_ci`
  while Python and PHP compare bytes - 250 newsroom URLs carry a capital, and
  the two paths disagreed before it did. Verified against the live grid: 58
  column/direction pairs, 24 filter/sort combinations and 4 paging tilings.
- **One page, whatever fetched the rows.** `src/scrapev3/status_view.html` is
  rendered by both `scrapev3 status --html` (pymysql) and
  `clients/status_demo.php` (PDO, or a JSON fixture), from the same
  `{generated_at, summary, agencies}` payload. Producers substitute `__DATA__`
  and `__NOTE__` and nothing else - the page builds its own header from the
  payload. Two renderers kept in step by hand drift exactly like two definitions
  of "healthy" would, and the first pair here disagreed about which columns
  existed within a day. The template ships as package data (`pyproject.toml`),
  so it survives being installed.
- **`agency_status` carries the verdict and the inventory, in one row.** A
  parallel table keyed on the same `a_id`, written by the same pass from the
  same two stores, would only buy a join and a way to be half-published. The
  columns are appended, never interleaved, and both clients select by name, so
  widening it does not break a website nobody has redeployed - but `_DDL` is
  `CREATE TABLE IF NOT EXISTS`, so **anything added after the first deployment
  must also go in `_MIGRATIONS`** or it appears only on new installs.
  `tests/test_status.py` pins that the two lists agree. Two distinctions carry
  weight here as well: `last_stored_at` (when *we* pulled a document) is not
  `last_article_at` (the publisher's own date), and `tns_pending` - stored
  locally, never landed in `press_release` - is a third silent failure that
  every count above it reports as fine.
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
- **`robots_agent` decides what we obey; `user_agent` only decides what we
  say.** `Protego.can_fetch` keys entirely on the string handed to it, so the
  two must never be the same field. 55 of 1,747 targets 403 the bot UA and
  serve a browser one (`defense.gov`, `weforum.org`, `michigan.gov` — verified
  with TLS and every other header held constant), while their robots.txt
  returns `can_fetch = True` for us. The browser string is therefore a repair
  path: tried once, only after a 403 or a wall, remembered per domain, with
  `From:` sent either way. Point robots matching at the sent UA and presenting
  a browser string silently widens what we are allowed to fetch — that is the
  regression `tests/test_identity.py` exists to catch. Leading with the honest
  UA is also what keeps Cloudflare Verified Bots reachable.
- **`status = 0` is four different things.** A transport exception, an open
  circuit breaker, a robots refusal, and a `Response` nobody filled in.
  `fetch.failure_kind` is the closed vocabulary over them, closed for the same
  reason `severity` is. The audit recorded `resp.wall` and had *no branch* for
  `resp.error`, so 108 of 149 unreachable targets reached the website with
  their reason discarded and were scored as the publisher's fault — including
  20 `.mil` agencies that only our own resolver could not resolve. A `robots`
  refusal is not a finding; `dns` and `circuit` are findings against us.
- **The browser tier is pointed at rendering, not at refusals.** Off unless
  `SCRAPEV3_BROWSER=on`, and it escalates only a domain the frontier already
  marked `js_rendered` — a site that never refused us and simply renders its
  articles in JavaScript. `challenge` needs a second switch
  (`SCRAPEV3_BROWSER_CHALLENGES`) because a challenge page *is* a site
  declining an identified crawler. `refused` is never escalated: of 41 walls,
  30 were flat `access denied`, which Chrome renders identically. Every render
  runs inside `PoliteFetcher._paced` — the same context manager `_raw_get`
  uses — so politeness is structural, and the gate sits in `_get_once` *below*
  the robots check so there is no second route to a disallowed URL.
  After the DNS and identity fixes the whole corpus had **8** genuine walls
  left, so measure before investing here.
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
