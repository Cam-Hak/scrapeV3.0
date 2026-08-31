# Talking to the crawler from another codebase

The website and the crawler are separate projects, possibly on separate
machines. They integrate through **two tables** in the shared MySQL — not an API,
not a socket, not a shared filesystem.

| Direction | Table | Who writes | Who reads |
|---|---|---|---|
| Remove an agency | `scrapev3.removed_agency` | the website | the crawler |
| Request a site | `scrapev3.requested_site` | the website | the crawler |
| Show crawl health and inventory | `scrapev3.agency_status` | the crawler | the website |
| Track what is going wrong | `scrapev3.crawl_fault` | the crawler | the website |

Neither side calls the other. Both are reads and writes against shared state, so
there is no service to run, no port to hold open, and nothing to restart when
the other side is down.

| File | What it is |
|---|---|
| `remove_agency.php` / `.py` | Write side: record a removal |
| `request_site.php` / `.py` | Write side: ask for a site to be crawled |
| `status.php` / `.py` | Read side: health *and* inventory, **data only, no rendering** |
| `faults.php` / `.py` | Read side: what is going wrong with the crawl, ranked |
| `example.php` | **Start here.** The fetch, then a table to replace with your own |
| `status_demo.php` | A throwaway page for looking at the data. Not for production |

---

# Removing an agency

The website inserts a row. The crawler purges that agency from the frontier, the
dedup index, the JSONL archive and `tns.press_release` at the start of its next
pass, and `scrapev3 seed` refuses to re-add it.

## The contract

```sql
INSERT INTO scrapev3.removed_agency (a_id, removed_at, note)
VALUES (?, UTC_TIMESTAMP(), ?)
ON DUPLICATE KEY UPDATE note = VALUES(note);
```

That is the whole interface. Anything that can reach MySQL can do it — the
snippets here are conveniences, not a required library.

**`ON DUPLICATE KEY UPDATE` matters.** `a_id` is the primary key, so submitting
the same removal twice is harmless rather than an error. The website should
never need to check whether an agency is already removed.

## Setup, once

The crawler creates the schema and table on its first run. To create them from
the website side instead:

```sql
CREATE DATABASE IF NOT EXISTS scrapev3 CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS scrapev3.removed_agency (
  a_id        INT NOT NULL,
  removed_at  DATETIME NOT NULL,
  note        VARCHAR(255) NULL,
  PRIMARY KEY (a_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Give the website its own user, with rights to these two tables and nothing else:

```sql
CREATE USER 'website'@'%' IDENTIFIED BY '...';
GRANT SELECT, INSERT ON scrapev3.removed_agency TO 'website'@'%';
GRANT SELECT, INSERT ON scrapev3.requested_site TO 'website'@'%';
GRANT SELECT         ON scrapev3.agency_status  TO 'website'@'%';
GRANT SELECT         ON scrapev3.crawl_fault    TO 'website'@'%';
```

`SELECT` on `removed_agency` is there so the site can show what it has already
submitted. It deliberately has no `DELETE`: undoing a removal is an operator
action (`scrapev3 remove --restore`), not something a web request should be able
to do. `agency_status` is **read-only to the website** for the same reason in
reverse — health is the crawler's finding, and a site that could write to it
could show a green grid over a crawler that stopped weeks ago.

## What happens next

Nothing immediately. The crawler is not listening — it reads the list when it
starts a pass, so a removal takes effect on the next run rather than the next
second. To apply one right away, on the crawler machine:

```bash
scrapev3 remove --apply
```

## Timing and identity

- **`removed_at` is UTC.** The crawler writes `UTC_TIMESTAMP()`; use the same so
  the column does not end up mixing zones.
- **`a_id` is the agency id**, the same one in `data/sites.csv` and
  `tns.agencies` — not a domain. One domain can carry hundreds of agencies
  (`house.gov` carries 417), and removing a domain would take all of them.

---

# Requesting a site

The mirror of removing one. The website inserts a row; the crawler seeds that
newsroom URL into the frontier at the start of its next pass, and crawls it from
then on like any other target.

## The contract

```sql
INSERT INTO scrapev3.requested_site (newsroom_url, a_id, requested_at, note)
VALUES (?, ?, UTC_TIMESTAMP(), ?)
ON DUPLICATE KEY UPDATE a_id = VALUES(a_id), note = VALUES(note);
```

That is the whole interface, and the same rules as `removed_agency` apply to it:
`ON DUPLICATE KEY UPDATE` makes a repeat submission harmless, `requested_at` is
UTC, and nothing happens until the crawler's next pass. To apply one right away,
on the crawler machine: `scrapev3 request --apply`.

```sql
CREATE TABLE IF NOT EXISTS scrapev3.requested_site (
  newsroom_url  VARCHAR(768) NOT NULL,
  a_id          INT NOT NULL,
  requested_at  DATETIME NOT NULL,
  note          VARCHAR(255) NULL,
  PRIMARY KEY (newsroom_url),
  KEY idx_requested_agency (a_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**The key is the URL, not the agency.** One agency can own several newsroom
pages, and the frontier's unit is the page rather than the publisher, so
requesting a second newsroom for an agency that already has one adds a target
rather than replacing it. Re-requesting the *same* URL under a different `a_id`
moves it: the frontier holds one owner per newsroom, so correcting which agency
a page belongs to has to be expressible.

## A removal outranks a request

The website owns both write tables, so it can ask for two incompatible things.
An `a_id` on `removed_agency` that also appears here is **refused, every pass**,
and the refusal is counted rather than skipped quietly.

Without that rule the request wins by accident and keeps winning: the crawler
purges the agency, seeds it again from the request list on the next pass, purges
it again, forever. A publisher who asked to be taken out would stay in the
crawl, and the only trace would be a removal that never seems to finish.

So a site that offers both actions should treat "remove" as final. Putting an
agency back is an operator action — `scrapev3 remove --restore A_ID` on the
crawler machine — not a second form on the website.

## Send the newsroom page, not the home page

**`newsroom_url` is the index that lists press releases.** Discovery starts at
this URL and works outward: it looks for a feed the page declares, a CMS API
under the same host, a sitemap section scoped to it, and failing all three the
links on the page itself. A home page gives it the whole site to guess from and
a single article gives it nothing to guess from — both usually resolve to
`empty` on the grid, which looks like a crawler fault rather than a bad URL.

**Do not send a domain.** There is no column for one. The crawler derives the
registrable domain itself because that value is its pacing key and its shard
key: a value supplied from outside that disagreed would either split one
publisher across two workers or hammer it, and neither fails loudly.

## What being on the list does not mean

It does not mean the site is being crawled. It means it has been asked for.
`agency_status` is where the answer is — a requested site that never produced
anything shows up there as `empty` or `never`, with the reason.

---

# Reading crawl health

`agency_status` holds one row per agency and answers two different questions.
The **verdict** — is it working, when did it last work, how much has it produced
— and the **inventory**: is its discovery solved and by what, when did we last
actually pull a document, when does the schedule come back. The website reads it
and draws the grid. A refresh button is that `SELECT` run again — there is no
request to the crawler, nothing to wait on, and no way for a page load to slow a
crawl down.

**Select columns by name, never `*`.** Both shipped clients do. The row grows —
it has already gone from 17 columns to 29 — and a page that selects by position
starts reading the wrong values the first time it does.

```php
require 'status.php';

$pdo  = scrapev3_connect($host, $user, $password);
$grid = scrapev3_grid($pdo);          // summary counts + every agency, worst first
$one  = scrapev3_status($pdo, 22385); // or null if the crawler does not hold it
```

`status.php` emits **nothing**. Every function returns an array; including the
file produces no output, so it is safe inside a JSON endpoint or before headers
are sent. `status.py` is the same interface for a Python site.

**Copy `example.php` and delete its markup.** It is the whole integration in one
file — connect, fetch with filters and a sort, loop. Everything below the fetch
is a plain table so the snippet runs; it is not a suggested layout. Three things
in it are worth keeping: colour from `severity`, `reason` printed verbatim, and
`updated_at` on the page.

## Sorting

Pass a column name. Anything not in `SCRAPEV3_SORTABLE` throws rather than
reaching the SQL, so a sort key straight out of `$_GET` is safe:

```php
$rows = scrapev3_statuses($pdo, ['sort' => $_GET['sort'] ?? null,
                                 'desc' => isset($_GET['desc'])], 100);
```

Three guarantees, each of which is a bug if it is missing:

- **Nulls last, both directions.** MySQL puts them first ascending. "Never
  pulled a document" is not the smallest date — it is the absence of one, and a
  grid sorted oldest-first that opens on a screen of blanks is useless.
- **A total order.** `a_id` breaks every tie, always ascending. Sorting by
  `health` leaves two thousand rows tied, and without a tiebreak the same page-2
  query returns rows that were already on page 1.
- **`severity` sorts by rank, not alphabetically.** Alphabetical reads "error,
  ok, warn" — the one order in which the broken sites are not first.

`scrapev3_sort_rows()` applies the same order to rows from a JSON fixture, for a
site with no database. It folds case, because `agency_status` is
`utf8mb4_0900_ai_ci` and MySQL sorts "a" before "Z" where PHP and Python sort on
bytes — 250 of the live newsroom URLs carry a capital, so the two paths returned
different orders before it did. Accents are not folded; `ai_ci` also equates "e"
and "é", which would need full Unicode collation to reproduce.

## The verdict is the crawler's, not the template's

Each row already carries its answer:

| Column | Use it for |
|---|---|
| `health` | the **label** — `healthy`, `quiet`, `empty`, `stale`, `blocked`, `failing`, `never`, `disabled` |
| `severity` | the **colour** — only ever `ok`, `warn`, `error` |
| `reason` | the **tooltip** — a ready sentence, e.g. "3 crawls in a row failed" |

`scrapev3 status --json` also puts `recent_days` in the `summary` block, so a
page can label the `articles_recent` column with the crawler's actual window
instead of a hardcoded 30. `scrapev3_summary()` reads MySQL and cannot know it,
so a consumer should treat it as optional and default to 30.

And the inventory half, which carries no verdict — these are facts to show, not
conditions to colour:

| Column | What it answers |
|---|---|
| `targets_cached` vs `targets` | How many of the agency's newsroom pages discovery has solved |
| `discovery_method`, `feed_url` | What is cached, and where it points |
| `feed_absent`, `probed_at` | We looked for a feed, and there is not one |
| `conditional_get` | An etag or last-modified is armed, so an unchanged page costs nothing |
| `last_stored_at` | When **we** last put a document in the index |
| `first_stored_at` | When this agency first produced anything |
| `next_due_at`, `crawl_delay_s`, `revisit_period_s` | When the schedule comes back, and how politely |
| `tns_loaded`, `tns_pending` | How many of its articles reached `press_release` |

**Switch on `severity`, display `health`.** `severity` is a closed three-value
vocabulary and is safe to branch on. `health` is open: the crawler may learn a
new word for a new kind of fault, and a page that maps health words to colours
itself would render that one uncoloured — which reads as "fine". An unknown word
resolves to `warn`, so a new fault shows up as a problem on a site that has not
been redeployed.

Re-deriving health in PHP is the thing to avoid. The rules know what a
consecutive failure costs and what the retry backoff is; a copy in a template
drifts, and then the dashboard and the crawler disagree about which sites work.
To change what counts as healthy, edit `src/scrapev3/status.py`.

## The two that are easy to get wrong

- **`quiet` is not a fault.** It means we crawl the site fine and the publisher
  has not posted in months. Institutional newsrooms do that routinely, so
  colouring it red makes the grid cry wolf on most of the corpus. Its severity
  is `ok`.
- **`empty` is a fault, and the important one.** We reach the site, discovery
  answers, and nothing survives to storage. That is the silent failure the
  project exists to surface — 92 of the first 324 agencies crawled were in it —
  and it is deliberately not folded into `stale`, which means something else
  (we stopped reaching the site at all).
- **`last_stored_at` is not `last_article_at`.** The second is the publisher's
  own date on the newest thing we hold; the first is when we last put anything
  in the index. They disagree in both directions that matter: a newsroom
  republishing 2019 items looks fresh by the publisher's date, and an agency we
  quietly stopped storing looks fine by it too. Show ours when the question is
  "is the crawler working", theirs when it is "is the newsroom alive".
- **`tns_pending` is a third gap, after `empty` and `stale`.** Stored here,
  never landed in `press_release`. Every count above it — `articles`,
  `articles_recent`, even `health` — says the agency is fine, because from the
  crawler's side it is.
- **`targets_cached < targets` is not a health word.** An agency can be
  perfectly healthy and still own a newsroom the cascade has never solved. It is
  the list worth looking at when deciding what needs work, and no `severity`
  will point you at it.

## Freshness

**Show `updated_at`.** It is when the crawler last wrote the row. The table is
refreshed by a batch job that can simply stop running, and a frozen grid looks
exactly like a healthy one. `scrapev3_summary()` returns the newest one.

## Setup, once

The crawler creates the table. Publishing is off by default — set
`SCRAPEV3_STATUS=on` in the crawler's `.env` and every finished pass refreshes
the grid. To refresh it by hand, or the first time:

```bash
scrapev3 status --publish
```

---

# Tracking what is going wrong

Two questions, two tables, and conflating them is the mistake to avoid.
`agency_status` answers **"is this publisher being collected?"** — one row per
agency, safe on a page a publisher might see. `crawl_fault` answers **"what is
wrong with the crawler?"** — one row per failure kind across the whole corpus,
for whoever operates it. `dns × 20` is our operational detail and means nothing
to a newsroom, so do not put it on a publisher-facing page.

```php
require 'faults.php';

$worst = scrapev3_faults($pdo);           // ranked, worst first
$mine  = scrapev3_faults($pdo, 'us');     // the to-do list
```

Each row already carries its own verdict, so nothing is re-derived:

| Column | Use it for |
|---|---|
| `kind` | the **label** — `dns`, `tls`, `http_4xx`, `discover_failed`, `extract_body_is_chrome`, … |
| `owner` | **whose problem** — `us`, `site` or `policy`, and the only closed one |
| `severity` | 1, 2 or 3 — the same scale the audit uses |
| `score`, `band` | the **ranking** — order by `score`, colour by `band` (`urgent`/`notable`/`minor`) |
| `domains`, `occurrences` | how wide, and how many |
| `example_domain`, `sample_detail` | one site and one message, so a row is actionable |

**Order by `score`, not by `occurrences`.** The crawler ranks on severity × how
many *domains* raised it × whose problem it is. One site 404ing forty URLs and
twenty sites failing once each are the same total and completely different
problems, and sorting on volume puts the wrong one first. Re-deriving the rank
in PHP would be a second definition of "worth fixing" that drifts from the
crawler's — the same trap as re-deriving `health`.

**`policy` rows score 0 and never lead.** A robots.txt we obeyed and a bot wall
are returned so they can be counted, and they are weighted to nothing so they
cannot fill the top of a work queue. Hide them by default; show them under a
toggle.

**It is a snapshot, not a log.** The table is rewritten each pass and pruned of
anything that stopped happening, so a kind fixed last week disappears rather
than lingering. History lives on the crawler — `scrapev3 faults --runs 7`. Show
`updated_at` for the usual reason: a batch job that stops running leaves a
tracker that looks exactly like a quiet week.

## And per agency, with no second query

`agency_status` gained two columns, so the grid you already read says what
specifically broke:

```php
$row = scrapev3_status($pdo, 22385);
$row['fault_kind'];     // 'dns'
$row['fault_detail'];   // 'DNSError: getaddrinfo failed'
```

`reason` explains the *verdict* and can only ever restate the counters behind it
— "3 crawls in a row failed". These two carry the *cause*. They are set only for
agencies the last pass actually touched: an untouched row keeps what it last
reported, because "no fault this pass" and "not crawled this pass" are different
things.

Unlike the ranked list, a `policy` refusal **is** reported here. On one agency's
row "the publisher declined us" is the answer, not noise.

---

## Looking at the data before wiring any of it up

```bash
scrapev3 status --json data/status.json   # on the crawler machine
php -S localhost:8000 -t clients          # then open /status_demo.php
```

With no database configured, `status_demo.php` renders that file. Set
`SCRAPEV3_DB_HOST`, `SCRAPEV3_DB_USER` and `SCRAPEV3_DB_PASSWORD` and it renders
live rows instead. It says which of the two it is doing, on the page.

**Both go through `status.php`.** The fixture path is
`scrapev3_grid_from_file()`, the same function a staging site with no database
access uses — so looking at the data exercises the contract rather than going
around it. The page contains no SQL of its own.

**Three depths, one table.** The page opens on six columns — a_id, site,
health, recent volume, last pulled, and the `reason` sentence — which is what
someone checking on a publisher needs. **More columns** adds the cascade detail
(source, failure counter, pages, not-loaded, next due), and clicking any row
shows every field in the payload, unselected and unrenamed, so a column added
upstream appears there without this page changing. The choice is remembered per
viewer in `localStorage`.

**The fetch is the only thing that varies.** `status_demo.php` contains no
markup, no CSS, no severity-to-colour table and no column list. The page is
`src/scrapev3/status_view.html`, and `scrapev3 status --html` renders the same
file from the same payload — PDO, pymysql or a JSON file, one page. The whole
of the PHP is: get the grid, substitute it into `__DATA__`, and put a line
about where it came from in `__NOTE__`.

```bash
scrapev3 status --html    # the same page, filled by the crawler instead
```

That is the reason to keep it this way. An earlier version had one renderer here
and another in Python, and the two disagreed about which columns existed within
a day of being written — the same drift the `health` rules exist to prevent, one
layer up.

`status_demo.php` is still a **testing page**: it reads the template out of the
source tree, so a copy of `clients/` on its own web server will not find it. Copy
`status.php` into the real site and render however the site wants.
