# Talking to the crawler from another codebase

The website and the crawler are separate projects, possibly on separate
machines. They integrate through **two tables** in the shared MySQL — not an API,
not a socket, not a shared filesystem.

| Direction | Table | Who writes | Who reads |
|---|---|---|---|
| Remove an agency | `scrapev3.removed_agency` | the website | the crawler |
| Show crawl health | `scrapev3.agency_status` | the crawler | the website |

Neither side calls the other. Both are reads and writes against shared state, so
there is no service to run, no port to hold open, and nothing to restart when
the other side is down.

| File | What it is |
|---|---|
| `remove_agency.php` / `.py` | Write side: record a removal |
| `status.php` / `.py` | Read side: crawl health, **data only, no rendering** |
| `status_demo.php` | A throwaway page for looking at the data. Not for production |
| `sample_status.json` | Real rows from a crawl, so the demo page runs with no database |

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
GRANT SELECT         ON scrapev3.agency_status  TO 'website'@'%';
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

# Reading crawl health

`agency_status` holds one row per agency: is it working, when did it last work,
and how much has it produced. The website reads it and draws the grid. A refresh
button is that `SELECT` run again — there is no request to the crawler, nothing
to wait on, and no way for a page load to slow a crawl down.

```php
require 'status.php';

$pdo  = scrapev3_connect($host, $user, $password);
$grid = scrapev3_grid($pdo);          // summary counts + every agency, worst first
$one  = scrapev3_status($pdo, 22385); // or null if the crawler does not hold it
```

`status.php` emits **nothing**. Every function returns an array; including the
file produces no output, so it is safe inside a JSON endpoint or before headers
are sent. `status.py` is the same interface for a Python site.

## The verdict is the crawler's, not the template's

Each row already carries its answer:

| Column | Use it for |
|---|---|
| `health` | the **label** — `healthy`, `quiet`, `empty`, `stale`, `blocked`, `failing`, `never`, `disabled` |
| `severity` | the **colour** — only ever `ok`, `warn`, `error` |
| `reason` | the **tooltip** — a ready sentence, e.g. "3 crawls in a row failed" |

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

## Looking at the data before wiring any of it up

```bash
php -S localhost:8000 -t clients     # then open /status_demo.php
```

With no database configured, `status_demo.php` renders `sample_status.json` —
real rows from an actual crawl. Set `SCRAPEV3_DB_HOST`, `SCRAPEV3_DB_USER` and
`SCRAPEV3_DB_PASSWORD` and it renders live rows through the same code path. It
says which of the two it is doing, on the page.

Regenerate the fixture from the current crawl at any time:

```bash
scrapev3 status --json clients/sample_status.json
```

`status_demo.php` is a **testing page**. Copy the severity-to-colour lookup and
the habit of printing `reason` verbatim; do not copy its layout, its inline
styles, or its practice of catching every exception and carrying on.
