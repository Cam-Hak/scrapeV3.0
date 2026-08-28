# scrapev3

Layout-agnostic news and press-release scraper. Collects **headline, body text,
and publish date** from 5,000–50,000 sites at daily cadence, with **no
hand-written per-site CSS/XPath selectors**.

Full design: `~/.claude/plans/large-scale-web-proud-snail.md`

## Why this exists

The predecessor system's own logs show the problem plainly: **368 of ~480
logged extraction failures (77%) were "a CSS class no longer matches."** Not
fetching, not blocking — selector rot. And it fails *silently*: a redesigned
page returns HTTP 200 and yields a document with the nav menu as the body.
Adding sites was 36% of all commits.

v3 attacks that structurally:

| Layer | Approach |
|---|---|
| Discovery | RSS → Google News sitemap → sitemap → CMS JSON API → listing scrape |
| Extraction | JSON-LD → OpenGraph → trafilatura (body) → htmldate (date) |
| Hard tail | LLM induces **CSS selectors once per domain**, grounded and validated, then cached and reused for free |
| Drift | Per-domain body-length baselines catch silent layout changes |

Nothing here is a paid service. **No LLM API, no proxies, no managed browser,
no cloud VM.** The local model runs on Ollama.

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -e .[sink]    # Windows
# .venv/bin/pip install -e .[sink]      # WSL2 / Linux
# [sink] adds PyMySQL and Unidecode, needed only for tns.press_release

cp .env.example .env                    # then edit
scrapev3 doctor                         # verify the environment
```

> **Run under WSL2 for production.** Windows' default `ProactorEventLoop` does
> not implement `add_reader`, which curl_cffi and aiodns need. The CLI switches
> to the selector loop automatically, but that caps at 512 file descriptors.

## Current status

**Phase 1 complete — the survey tool.** Before building the crawler, the plan
requires measuring the actual target universe, because five load-bearing
numbers were flagged as unverified inference.

```bash
scrapev3 survey data/sample_sites.txt --limit 1000 --concurrency 16
```

Takes a file of newsroom URLs (`.txt` one-per-line, or `.csv`), runs the real
discovery and extraction path against each, and writes one JSONL row per domain
plus a summary. It answers:

1. **JS-escalation rate** — does plain HTTP yield article text?
2. **JSON-LD coverage** — the "68% of media sites" figure circulating in 2026
   is SEO content marketing with no primary source. Measure it.
3. **`articleBody` completeness** — how often structured data carries the body.
4. **`/wp-json` reachability** — no published measurement exists.
5. **Discovery coverage** — how the cascade actually stacks up.

Plus shared-IP concentration, which decides whether per-domain pacing is
enough or whether we also need to pace per IP.

Results are flushed per row, so an interrupted survey is still usable.

### Target corpus

`scrape_test.csv` (the v2 site list) yields, via `data/sites.csv`:

| Measure | Value |
|---|---|
| Newsroom URLs | **2,401** (6 exact duplicates dropped) |
| Distinct registrable domains | **1,747** |
| Top TLDs (by URL) | .edu 637, .gov 632, .org 522, .com 339, .uk 69, .ca 46, .mil 41 |

**2,401 URLs collapse to 1,747 domains, and the collapse is lopsided:**
`house.gov` alone accounts for **417** URLs and `senate.gov` for **80** — individual
legislators' press pages on shared infrastructure.

That matters for politeness, so it was checked rather than assumed. All 24
sampled `house.gov` hostnames resolve to **one Akamai IP** (`23.213.78.250`);
`senate.gov` spreads over 5 IPs, all Akamai. So pacing on the registrable
domain is *correct* there — those really are one origin, and serializing them
protects a shared CDN edge instead of wasting time. `canada.ca` genuinely
splits across separate origins, but at 18 URLs the over-serialization costs
about four minutes a day. **Conclusion: keep eTLD+1 as the pacing key.**

Regenerate any time with `python scripts/extract_sites.py`, which reads only
the agency id and URL columns and ignores v2's 18 columns of selectors:

- `data/sites.csv` — `a_id, newsroom_url, domain` for all 2,401 targets
- `data/sites.txt` — URLs only
- `data/survey_sample.txt` — 1,000 randomly sampled distinct domains (seed 20260826)

### Phase 1 results — 1,000 domains surveyed

A random sample of 1,000 distinct domains, 26 minutes wall clock, full
politeness. Five numbers the plan had flagged as unverified inference are now
measured (`data/surveys/phase1.summary.json`).

**Reachability**

| Metric | % |
|---|---|
| Reachable (HTTP 200) | 90.9 |
| Bot walls (Cloudflare etc.) | 1.6 |
| robots.txt disallows us | 1.9 |

**HTTP-first is validated.** Only **8.1% need a browser** — mid-range of the
5–15% the research predicted, and median plain-HTTP yield is **2,966 chars** of
article text. A further 2.0% ship a hydration payload, where the body is
already in the HTML as JSON and can be mined without a browser.

**Structured data is far rarer than the literature claims** (860 real article
pages tested):

| Field | Measured | Commonly claimed |
|---|---|---|
| JSON-LD article type | **23.1%** | "~68% of media sites" |
| `headline` | 22.6% | |
| `datePublished` | 22.3% | |
| **`articleBody`** | **1.6%** | |

The circulating "68%" figure is SEO content marketing with no primary source.
Planning against it would have badly oversized the metadata path. `articleBody`
is effectively absent, so **trafilatura carries the body essentially alone** —
metadata is a headline/date accelerant only.

Of the article-type JSON-LD found: `NewsArticle` 12, `Article` 12,
`BlogPosting` 2. **Matching only `NewsArticle` would have missed 48% of it.**
`WebPage` appears on ~40% of pages and inherits `datePublished`, so it is now
accepted for date/headline at lower confidence — never for body.

**Discovery — WordPress is the top mechanism, not the fallback**

| Mechanism | % of domains |
|---|---|
| Any sitemap | 82.2 |
| RSS via autodiscovery | 34.5 |
| RSS via path probe | 17.1 |
| …feed carries full `content:encoded` | 23.1 |
| **CMS JSON API (`/wp-json`)** | **30.8** |
| Google News sitemap | 0.5 |
| Listing page only (last resort) | 9.0 |

WordPress on **37.4%** of domains, and **82.4% of those expose a usable
`/wp-json`** — one request returning headline, full body HTML and exact
`date_gmt`. Google News sitemaps are essentially absent, which fits: this
corpus is institutional `.edu`/`.org`, not news publishers. Path probing found
a third of all feeds, so autodiscovery alone would have left real coverage
behind.

**Shared IPs — this one changed the design.** 28.3% of domains share an IP with
another domain, largest cluster **28**. Those clusters are managed-hosting
edges (WP Engine `141.193.213.x`, Pantheon `23.185.0.x`) fronting hundreds of
unrelated customers. Since each is a different registrable domain, per-domain
pacing alone would have let all 28 burst at one edge at once. Hence the
secondary per-IP concurrency cap described below.

## Running it

```bash
# one-time
python scripts/extract_sites.py            # scrape_test.csv -> data/sites.csv
scrapev3 seed data/sites.csv               # load 2,401 targets into the frontier

# every run
scrapev3 doctor                            # environment check
scrapev3 frontier                          # what is due
scrapev3 crawl --domains 10 --max-articles 5
scrapev3 show --limit 20                   # table of what was scraped
scrapev3 show --full --limit 3             # with body text
scrapev3 show --domain uta.edu --full      # one publisher
```

Output lands in `data/articles/articles-YYYYMMDD.jsonl`, one JSON object per
article: headline, body, `published_at`, plus provenance (`date_source`,
`body_source`, `headline_source`) and quality signals. `data/articles.sqlite`
is the dedup index.

**`crawl` is deliberately slow.** 5s per host, one concurrent request per host,
so ~10 domains takes several minutes. That is the politeness requirement
working. `--concurrency` raises how many *domains* run in parallel; per-host
pacing never changes.

### Bugs the first live runs exposed

Every one of these was a silent quality failure - plausible-looking records
that were wrong. They are the reason the extraction path is measured rather
than trusted, and each now has a regression test.

| Bug | Impact | Fix |
|---|---|---|
| **Listing page stored as an article** | `battelle.org`'s own newsroom URL was stored with the site's nav text as its body | A 3-segment path scored +0.5 for "deep path" with no article signal, and 0.5 > 0 passed. Classifier now requires a *positive* signal; section indexes (`/newsroom`, `/press-releases`) are rejected; sitemap URLs go through the classifier instead of bypassing it |
| **Wrong publisher attribution** | 4 of 28 articles (14%) stored Politico/WBUR/Courthouse News content under `ufw.org` with UFW's agency id | `ufw.org` runs a press-clipping feed. Refs whose registrable domain differs from the target are now rejected outright |
| **Headline included site chrome** | `"Center for Food Safety \| Press Releases \| \| Lawsuit Filed to Stop..."` | Splitting on the last separator assumes `Headline \| Site`; this site uses `Site \| Section \| Headline`. Now splits on every separator, drops site name and section chrome, keeps the longest survivor |
| **Body provenance mislabelled** | Feed/`wp-json` bodies were credited to `trafilatura` | Would have hidden a source change from drift monitoring. Now attributed to `feed` / `cms_api` |
| **Language detection silently dead** | Every article had `language: null` | `fast_langdetect` changed API; the call raised `TypeError` and a bare `except Exception` swallowed it. Now handles both API shapes explicitly |
| **Non-news content from site-wide feeds** | `edisonohio.edu`'s `/News` feed carried `/event/2026-08/welcome-week`, a campus event | Institutional sites often run one feed mixing press releases with events, staff profiles and courses. A non-news path veto now applies to *every* source, feeds included; plus section scoping prefers articles under the target's own path |
| **Nav menu stored as article body** | `ustravel.org/node/352363` body began "View the Main Menu ... Search U.S. Travel Association ... Find Members" | Added a `prose_ratio` signal (long-line fraction + sentence density). Calibrated on the real corpus: that nav body scores **0.016**, a static "about" page 0.254, genuine articles 0.44-1.00. Below 0.30 the article is unusable, not merely warned about |
| **A site-wide source outranked the target's own section** | `fightcancer.org/press-room/search` collected ten advocacy actions and events instead of its ten press releases, and reported success | Three sources could each fall back to "the whole site" and win. See below |
| **Slug threshold too strict** | Real articles like `/news/rewriting-story-metal` were rejected | Threshold lowered from 4 words to 3, which still separates them from 1-2 word section indexes |

### What ten consecutive 10-domain runs exposed

The first run of `crawl --domains 10 --max-articles 5` reported **15 failed**
with no reason recorded anywhere, plus nine "errors" of which eight were the
cascade working correctly. Ten runs later the same command reports **zero
errors and zero failures**. The individual bugs matter less than the pattern:
*every one of them was invisible in the output before it was fixed*, and the
first work of each round was making the run say what it had actually done.

| Bug | Impact | Fix |
|---|---|---|
| **Relative links resolved against the wrong base** | `ccu.edu/news` redirects to `www.ccu.edu/news/`, and its links are relative with no leading slash. Resolving them against the canonical URL — whose trailing slash canonicalisation had stripped — made `urljoin` treat `news` as a file, so every article URL lost its `/news/` segment. Twelve fetches, twelve 404s, zero articles, and the frontier recorded the domain as successfully crawled | Relative hrefs resolve against `resp.final_url`, never the requested URL. Six call sites: `harvest_links`, `from_listing`, `find_feed`, `from_feed`, `_covers_target`, and the audit's `page_links`. The corroboration gate had it too, so it was building its comparison set from URLs that exist nowhere and then reporting `no_overlap` — discovery blamed for a join bug |
| **`www.` stripped from the fetch URL** | Canonicalisation drops `www.`, which is right for identity and is not an assertion that the apex host serves anything. `ardian.com` has no A record at all; `escardio.org` resolves to a different host than `www.escardio.org` and fails TLS there; `law.georgetown.edu` does not resolve while `www.law.georgetown.edu` does | `get()` retries on the `www.` host, gated on DNS/TLS/connection failure — only when no server ever answered, so a real 404 is never re-requested. Not restricted to apex hosts: the shape of a hostname is not evidence about what resolves |
| **Redirects bypassed per-host pacing entirely** | `ersnet.org` 301s `/news-and-features/news/` to itself forever. curl follows redirects *internally*, so they never reach `_wait_turn` — at curl's default of 30 hops, ten article fetches became ~300 back-to-back unpaced requests to one host. No per-host delay could see it, because the delay is applied per `get()` call | `max_redirects` capped at 5, and the circuit breaker extended to transport failures: `_raw_get`'s exception path incremented `consec_failures` but returned before `_apply_backoff` ran, so repeated timeouts and redirect loops never backed off. Measured on that domain, ~300 requests down to 25 |
| **Refusals were paid for in full, every run** | `news.csub.edu` served a Cloudflare 403 to fourteen consecutive article fetches in one pass, each after the full per-host delay. The breaker existed but only opened on 429/503 | Opens after `SCRAPEV3_MAX_CONSEC_REFUSALS` consecutive refusals. A run of them is a verdict, not a transient fault |
| **Headline chrome stripped only from `<title>`** | `centerforfoodsafety.org` stored `Center for Food Safety \| Press Releases \| \| Lawsuit Filed...`. The fix for that exact string already existed, but ran only on the `<title>` path — and this site serves the same chrome in `og:title` | `strip_title_chrome` runs on every headline candidate. Deliberately conservative: a value with no recognised chrome is returned untouched, so an ordinary headline containing a dash is never truncated |
| **A headline belonging to a different page** | `lung.org` serves an `og:title` of `Press Releases \| American Lung Association` on every individual release. `headline_in_body` was **0.0** — the strongest available signal that the headline is not this article's | Decided on measured overlap, not on a rule about which tag to trust: near-zero coherence plus a materially better DOM `h1` swaps it, recorded in `quality.headline_replaced`. "Prefer the h1" is wrong just as often, and a test pins the case where the h1 is the section name and must not win |
| **The site name leaked into the headline** | Same site, no `og:site_name` to strip against, so releases stored `... \| American Lung Association` — which flows through to `press_release.headline` and the `$H` filename | The `<title>` gives it away: a CMS appending the site name to a title that already ends in it produces `Headline \| Site \| Site`. Only that doubling licenses the inference. Assuming the last segment of any title is the site name would truncate every real headline containing a dash |

**The diagnostics were the actual deliverable.** Four of the ten rounds were
spent making failures legible rather than changing crawler behaviour, and each
one immediately exposed a bug that had been running unnoticed.

| Before | After | What it found |
|---|---|---|
| `failed: 15` | `Why fetches failed`, by reason and by domain | `HTTP 403 x14` on one host, `TooManyRedirects` on another. A reason without attribution is half a diagnosis: `HTTP 404 x20` from one domain is a URL-construction bug, from twenty domains it is nothing at all |
| `unusable: 7` | `Why articles were unusable` | `body under 300 chars` and `body looks like page chrome` are different problems with the same count. `Article.usable` is now derived from `unusable_reason`, so the two cannot drift apart |
| Nine "errors" on a clean run | `errors` and `notes` on separate channels | A declined site-wide source is the cascade working. An error list that fills up on healthy runs is an error list nobody reads |
| Articles vanishing between `discovered` and `stored` | `older than the window` counted | 174 discovered, 36 stored and every rejection row zero looks broken. The honest answer was the age cutoff |
| `all discovery methods failed` | The status that caused it | `lawsociety.org.uk` returns a branded `403 - The Law Society`. That is a refusal, not a solvable challenge, so it is correctly not a bot wall — and the cascade ran every source against a site refusing us, then named the symptom and hid the cause |
| `circuit-open: host backing off x11` | `...after 5x TooManyRedirects` | A defect introduced by the breaker itself. The failures that tripped it happened during discovery, which does not feed the crawl's tally, so every article fetch afterwards saw only an open circuit — one silent failure replaced by another |

**What deliberately did not change.** `is_non_news_path` matches whole path
segments, so it does not catch `/live-online-courses/`. Extending it to
hyphenated compounds would also veto real press releases whose slug ends in
`-programs` or `-courses` — the per-site over-fitting this rewrite exists to
end. `classify_url` already rejects those URLs, so the gap costs nothing.

**Some errors are not bugs and will never reach zero.** `dol.gov`, `faa.gov`,
`cargill.com` and `stfx.ca` serve genuine bot walls; `um.edu.mo` returned 503.
Those are sites declining an identified crawler, which is their call. The bar
is that every remaining line is a true statement about the site rather than a
defect on our side.

Final corpus over those ten runs: **322 unique articles from 77 domains**,
median `prose_ratio` **0.848** with none below 0.30, median body 3,520
characters, and **zero** rows missing a date or a headline.

### When a whole-site source wins over the target's own section

The worst bug found so far, because nothing about it looked like a bug.
`fightcancer.org`'s press room is at `/press-room/search`; the releases
themselves live at `/releases/<slug>`. That split is a completely ordinary CMS
layout, and it defeated **three separate sources**, each of which fell back to
"the whole site" when it could not match the section — and each of which then
returned ten perfectly real articles. Wrong ten. No error, no warning, HTTP 200
throughout.

**The rule, stated once:** a source that answers for the *whole site* must
corroborate against the target's own page before it is allowed to win, and
whatever it returns must be mostly this publisher's own content. Three sites
needed three separate patches before that was made general — `fightcancer.org`
through a probed feed, `aacr.org` through the CMS API, `ufw.org` through a
press-clippings feed — which is the lesson worth keeping rather than the three
fixes.

| Source | What it did | Fix |
|---|---|---|
| **RSS** | Path-probing looks for a feed at the *site root*. It found the organisation-wide `/rss.xml`, whose newest items are advocacy actions, events and legislative summaries | A feed the page **declares** is authoritative. A **probed root** feed must corroborate: at least one of its items has to be linked from the target page. Zero overlap across a whole feed means it covers a different part of the site |
| **Sitemap** | No section match, so `refs = in_section or refs` widened to the entire site: `/what-we-do/…`, `/policy-resources/…` | An unscoped sitemap is now marked as such and held in reserve. The target's own listing page — an explicit statement of what belongs in this section — outranks it |
| **Listing harvest** | Hard-filtered to the section, so it found **zero** links and could not rescue the situation | Section first, then fall back to links **outside** `<nav>`/`<header>`/`<footer>`/`<aside>` |

That last one is worth dwelling on. On that page **43 links pass the URL
classifier**, and the classifier scores every single one of them **2.00** — by
URL shape, `/what-we-do/access-health-insurance` is indistinguishable from a
press release, and no amount of tuning the classifier would separate them.
Counting does not help either: `/what-we-do` contributes 14 links and
`/releases` only 10, so "pick the biggest cluster" picks the nav.

What does separate them is where they sit in the document:

| group | links | inside a nav landmark |
|---|---|---|
| `/releases` (the real ones) | 10 | **0 / 10** |
| everything else | 33 | **33 / 33** |

So the harvest fallback is structural — landmark elements, not a list of class
names. Guessing at class names is the per-site knowledge this project exists to
eliminate, and it is the thing that rots.

| **CMS API** | `/wp-json/wp/v2/posts` is as site-wide as `/rss.xml`. aacr.org's served `/blog/…` to a `/newsroom/news-releases` target; ufw.org's returned 28 press clippings from Courthouse News, HuffPost and the Washington Post out of 30 | Corroborated like any root source. And `usable()` now asks whether a source returned anything the crawl would *keep*, not merely anything at all — below 50% own-domain content, it is someone else's newsroom however real the articles are |

**The cache made it stick.** The frontier remembers the winning source so a
solved domain costs one request instead of fourteen — and a wrong answer is
exactly as sticky as a right one. A target that had already cached the
site-wide feed went straight back to it every run, with the corrected cascade
never getting a look in. Two changes: the fast path declines a **root-level**
feed on a **sectioned** target (a free check — it reads the URL, fetches
nothing), and `scrapev3 reset` now clears the cached source, conditional-GET
state and feed-absence verdict, so a reset really does mean "pretend we never
crawled this."

**And the listing page needed a fast path of its own.** Once a target settles
on `listing`, that page is the cheapest source there is — one request, and the
same request the full cascade opens with anyway. There was no such branch, so a
listing target re-walked the sitemap index on every run, re-establishing daily
what it had already established: measured on `fightcancer.org`, **8 requests
and 38s of discovery, 7 of them wasted**. With the branch in place the same
target costs **1 request, ~5s**, and still falls through to the full cascade if
the page stops yielding, so a site that changes layout is reconsidered rather
than written off.

## Testing discovery

Discovery is both the highest-value part of the system and the easiest place to
fail silently. `fightcancer.org` returned ten real articles - advocacy actions
and events from the organisation-wide feed - while its press room sat
untouched. Nothing errored. HTTP 200 throughout. No amount of checking status
codes would have caught it.

```bash
scrapev3 audit --limit 60             # sample the corpus, ranked worst-first
scrapev3 audit --domain usc.edu       # drill into one
scrapev3 audit --a-id 22385           # or one agency
```

Read-only: no article pages are fetched, and nothing is written to the dedup
index or to MySQL. It costs the newsroom page plus whatever the cascade spends.

**The ground truth needs no labelling.** A newsroom page is the publisher's own
statement of what belongs in that section, so the strongest available signal is
simply whether the URLs discovery returned appear as links on the page we were
pointed at. Zero overlap across a whole result set means discovery is looking
somewhere else - and that is computable across all 2,401 targets with nobody
labelling anything.

| Flag | What it means | Severity |
|---|---|---|
| `no_overlap` | none of the results are linked from the newsroom page | broken |
| `off_domain` | a result belongs to another publisher (the `ufw.org` failure) | broken |
| `no_articles` | discovery returned nothing | broken |
| `unreachable` | the newsroom page itself did not load | broken |
| `low_overlap` | under 20% of results appear on the page | suspicious |
| `scattered` | results spread across many paths rather than clustering under one | suspicious |
| `non_news_heavy` | over 30% look like events or staff pages | suspicious |
| `seed_echo` | discovery returned the newsroom page itself (the `battelle.org` failure) | check |
| `unscoped` | the source could not be narrowed to the target's section | check |
| `last_resort` | only the listing page worked - no feed, no usable sitemap | check |

Severity 3 is definitive on its own and is not outvoted by the absence of
milder flags; below that, small oddities accumulate. Each row records the
measurements as well as the verdict, so the scoring can be re-run over an
existing `data/audits/*.jsonl` without re-fetching anything.

One target per domain is sampled: auditing 417 `house.gov` pages tells you the
same thing 417 times and spends 417 requests on one origin.

## Loading into MySQL

**Phase 3.5 complete — the `tns.press_release` sink.** Pulled ahead of Phase 4
because quality baselines are easier to trust when you can see the documents an
editor would actually see.

```bash
scrapev3 doctor                             # now checks MySQL and the tns tables
scrapev3 tns status                         # coverage, row counts, what is pending
scrapev3 crawl --sink tns --dry-run         # compose the rows, write nothing
scrapev3 crawl --sink tns                   # crawl and load
scrapev3 tns show --full --limit 3          # read rows back out of press_release
scrapev3 tns backfill                       # load anything that never made it
scrapev3 tns backfill --resync              # after a TRUNCATE or a restore
```

To read the table directly, the client ships with the server but is not on
`PATH`:

```bash
"/c/Program Files/MySQL/MySQL Server 8.0/bin/mysql.exe" -u root -p tns
```

`SELECT * FROM press_release;` is not readable at a terminal — 38 columns and a
`body_txt` that runs to 20 KB. Use `\G` for one field per line, or ask for the
13 columns this sink actually writes:

```sql
SELECT * FROM press_release ORDER BY pr_id DESC LIMIT 2\G

SELECT pr_id, a_id, content_date, status, uname, location, filename,
       headline, CHAR_LENGTH(body_txt) AS body_chars
FROM press_release ORDER BY pr_id DESC LIMIT 20;
```

The other 25 columns stay NULL by design: `contact_info`, `orig_txt`, the six
`*_err_flag`s and every `*_sent`/`*_err` distribution field belong to
downstream, and v2 did not write them either.

> **`dbs/press_releases.sql` ends with `drop table` / `truncate press_release`
> / `select * from press_release`.** Running that file, or those lines, empties
> the table. The local dedup index will still believe those articles were
> loaded, so re-loading them needs `tns backfill --resync`, which re-checks
> every locally-`loaded` filename against the table and re-offers the ones
> whose row is gone.

`--sink tns` is additive: JSONL and the SQLite dedup index still get every
article. `SCRAPEV3_SINK=tns` makes it the default.

### Iterating on a site

**Revisit scheduling is off in the prototype** (`SCRAPEV3_SCHEDULE=off`). The
frontier does two separable jobs and only one of them is optional:

| | What it does | In the prototype |
|---|---|---|
| **The lease** | one worker owns a domain at a time | **always on** — per-host pacing is meaningless without it, since two workers on one domain each pace against their own clock |
| **The calendar** | `next_allowed_at`, revisit period, failure backoff | **off** — a domain crawled once was then unreachable for 24h, so "re-run that site" silently did nothing |

With the calendar off, every enabled domain is permanently due and crawling one
leaves it due. Cadence becomes the job of whatever invokes the crawler — cron,
a queue, a server loop — which is where it belongs once this runs unattended.
Set `SCRAPEV3_SCHEDULE=on` to restore it; the machinery is intact, not removed.

**This does not make the crawler less polite.** The 5s per-host delay, the
one-concurrent-request-per-host lock, the per-IP cap, jitter, `Crawl-delay` as
a floor and latency-adaptive backoff are all untouched — they are enforced
inside a pass, and the calendar only governed the gap *between* passes.
`scrapev3 frontier` says which mode it is in.

**Truncating `press_release` is still not enough on its own,** and the failure
is quiet. Two stores have to move together (three, with the calendar on):

| Store | What it remembers | If you skip it |
|---|---|---|
| `tns.press_release` | the loaded rows | — |
| `data/articles.sqlite` | every URL ever stored | `seen_url` runs *before* the fetch, so the re-run skips everything and stores nothing |
| the frontier calendar | when each domain is next due | only with `SCRAPEV3_SCHEDULE=on`: the domain is not offered again for 24h |

Truncate alone and the next run reports `already seen (skipped before fetch)`,
writes nothing, and looks like the sink is broken.

**The standard loop — clear one agency, re-scrape it, look at the result:**

```bash
scrapev3 reset --a-id 22385 --tns        # all three stores, scoped to one agency
scrapev3 crawl --a-id 22385 --sink tns   # re-scrape exactly that site
scrapev3 tns show --limit 10             # what landed
```

`reset --tns` deletes that agency's `press_release` rows, forgets its articles
locally, and makes its domain due. `crawl --a-id` then crawls exactly that
site. Nothing needs doing in Workbench.

**`reset` keeps the cached discovery source.** Re-deriving where a site's
articles come from means a cold cascade — nine feed probes and a sitemap index
walk, every one at the per-host delay — to arrive at the answer already on
file. The usual reason to reset is to re-check the *output*, not the discovery.
Pass `--relearn` when you have changed the cascade itself and want it
re-derived; expect it to take minutes rather than seconds.

`--a-id` bypassing the due queue is necessary, not a convenience: `acquire`
orders by `next_allowed_at`, and 1,747 never-crawled domains sort ahead of
anything re-crawled today, so a plain `crawl` after a reset leases somebody
else's domain. Only the *schedule* is bypassed — the lease still applies, and
per-host pacing is untouched, so a targeted crawl is exactly as polite as a
scheduled one. On a domain carrying many agencies (`house.gov` has 417
targets), `--a-id` crawls only that agency's own newsroom URLs.

> **Do not reach for `--refetch` when the rows are still in MySQL.** It forgets
> the local index, so the articles really are re-fetched — and then every one
> is rejected as `already loaded`, because the filename, agency and headline
> all match the rows still sitting there. You get a full crawl and an unchanged
> table. That is the duplicate guard working correctly, and it looks exactly
> like a broken scrape. `--refetch` is for the case where the rows are
> *already* gone — you truncated in Workbench — and you want to re-crawl in one
> command instead of two.

Wider scopes:

```bash
scrapev3 reset --domain usc.edu --tns    # one publisher
scrapev3 reset --tns --yes               # everything; --yes is required
```

`--tns` resolves the scope to explicit agency ids first; a scope that matches
nothing deletes nothing, rather than falling through to the whole table. The
JSONL archive is append-only and is never touched, so a re-crawl appends a
fresh line instead of rewriting history.

If you truncated in Workbench and only want the rows back — no re-fetching —
use `tns backfill --resync` instead: it replays from the JSONL archive and is
much faster than crawling again.

To check extraction without touching MySQL at all:

```bash
scrapev3 crawl --sink tns --dry-run    # composes the real rows, writes nothing
scrapev3 show --full --limit 3         # what the extractor actually got
```

A dry run deliberately does not record a load state, so a later real run still
picks the article up.

### The row

`tns` is not ours to redesign — it feeds the newswire CMS, and its schema, its
lookups and the shape editors read are all fixed. The INSERT is v2's column
list. The body is v2's template:

```
{lede}

* * *

{headline}
*
{body}

***

Original text here: {url}
```

Three fields come from `tns` itself, not from the page, and an article missing
any of them cannot be loaded:

| Column | Source | Example |
|---|---|---|
| `filename` | `agencies.filename` | `$H ams260826 New Rules` |
| lede + `location` | `agencies.leads` | `WASHINGTON, DATE -- The U.S. Department of Agriculture issued the following news release:` |
| `uname` | `url_grp.uname` via `agencies.ug_id` | `C22-SWmohanty` |

The lede template carries a literal `DATE`; the substituted date is AP style
with no year (`Aug. 26`), and `location` is the dateline the template opens
with. Load the three dumps in `dbs/` once and the whole directory is available:

```bash
mysql -u root -p < dbs/agencies.dmp.sql     # 34,392 agencies
mysql -u root -p < dbs/url_grp.dmp.sql      # 461 editorial groups
```

### What is identical to v2, and what is not

Identical: the column list, the body template, the `$H <prefix><YYMMDD><tail>`
filename including the space after `$H`, the AP month table, `headline` sliced
to 254 while the filename is built from the whole headline, the 100-word floor,
the 100–250-word "short doc" routed to status `W`, and `unidecode` on
everything — `press_release` is a **latin1** table, so a curly quote off a web
page is not untidy, it is unstorable.

Three deliberate departures, each covered by a test:

| v2 | v3 | Why |
|---|---|---|
| `location` left NULL | filled from the lede's dateline | Derivable from the same string the lede comes from |
| `re.sub(r"\s*\.", ".", body)` | `[^\S\n]*` instead of `\s*` | `\s` includes `\n`, so a paragraph beginning with punctuation lost its break. The intent was "horizontal whitespace" |
| `replace_defaults` strips `"Our Clemson"`, `"Latest News"`, `"Related Stories"` | only separators and press-release boilerplate | Per-site knowledge leaking into global code is the failure mode this rewrite exists to end. Per-domain boilerplate belongs in Phase 6, mined from the corpus |

### Two failure modes it does not inherit

**The filename is no longer the dedup key.** `press_release.filename` is
`UNIQUE`, and v2 both displayed it and deduplicated on it — so two same-day
articles from one agency whose headlines happened to end alike collided, and
the second was silently dropped. Here dedup is canonical-URL and content
hashes, and a genuine collision *widens the filename* (10 → 15 → 20 characters
of the headline's tail — exactly what v2's per-site `FILENAME CHARS` column did
by hand) until it is unique. A collision that is really the same document is
recognised by agency and headline and skipped.

**A database hiccup no longer loses the article.** The dedup index is written
before the insert, so without a load state a failed insert would leave the
article marked seen and unreachable forever — v2's fail-closed dedup. Every
article now carries `tns_state` (`loaded` / `rejected:<reason>` / `error`), a
rejection is a verdict and an error is retryable, and `scrapev3 tns backfill`
replays the difference from the JSONL archive.

### Coverage — 202 targets have nowhere to land

The frontier carries **2,399** distinct agency ids; `tns.agencies` knows
**2,197** of them. The remaining **202 (8.4%)** are sites added since the
agency data was last loaded, and 51 more resolve to `url_grp` `UNASSIGNED` with
`uname` `-1`. Those articles are scraped and stored to JSONL, counted in the
`no_agency` bucket, and not loaded. `scrapev3 tns status` prints the gap and
the first offending ids; closing it means adding agency rows, not code.

First run against a live crawl: **49 rows**, 17 agencies, 49 distinct
filenames, no non-ASCII bytes, every `a_id` joining to `agencies`, 39 status
`D` and 7 status `W`.

## Politeness

Not best-effort — enforced by construction and covered by tests:

- **Concurrency per host is 1**, held by a lock, keyed on the registrable
  domain (eTLD+1) — measured correct: all 24 sampled `house.gov` hostnames
  resolve to one Akamai IP.
- **Concurrency per IP is capped** (default 4) as a secondary constraint, for
  the inverse case the survey found: unrelated domains sharing a hosting edge.
- **±30% jitter** on every delay. At daily cadence the aggregate rate is ~1.5
  req/s across 50k sites; the only real risk is burstiness.
- **`Crawl-delay` is a hard floor**, never a ceiling.
- **Latency-adaptive backoff**: if a host's response time exceeds ~2× its
  rolling p50, the delay doubles *even on HTTP 200*.
- **Conditional GET** (ETag preferred over Last-Modified).
- Identifies honestly: stable UA containing `bot` plus a `+https://` contact
  URL, and a `From:` header.

`tests/test_politeness.py` proves the invariants against a live local server
that records request arrival and completion timestamps.

## Tests

```bash
.venv/Scripts/python -m pytest tests/ -q
```

## Roadmap

- [x] Phase 0 — scaffold, identity, secret hygiene
- [x] Phase 1 — survey tool, and the 1,000-domain survey run
- [x] Phase 2 — domain-lease frontier (SQLite now, MySQL ready)
- [x] Phase 3 — discovery cascade + extraction cascade + crawl loop + JSONL sink
- [x] Phase 3.5 — the `tns.press_release` MySQL sink, pulled ahead of Phase 4
- [ ] Phase 4 — quality baselines and drift detection
- [ ] Phase 5 — local LLM wrapper induction (Ollama) + review queue
- [ ] Phase 6 — post-processing (ftfy, keyword routing, MinHash dedup, per-domain
      boilerplate mining) on top of the sink that now exists

## Security

Both predecessor repos committed live credentials (a DB root password and an
SMTP password, the latter not even gitignored). `.gitignore` here is
deliberately broad — it is far cheaper to un-ignore one file than to scrub a
secret out of git history. **All configuration comes from `.env`, which is
never committed.**
