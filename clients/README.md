# Removing an agency from another codebase

The website and the crawler are separate projects, possibly on separate
machines. They integrate through **one table** in the shared MySQL — not an API,
not a socket, not a shared filesystem.

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

Give the website its own user, with rights to this one table and nothing else:

```sql
CREATE USER 'website'@'%' IDENTIFIED BY '...';
GRANT SELECT, INSERT ON scrapev3.removed_agency TO 'website'@'%';
```

`SELECT` is there so the site can show what it has already submitted. It
deliberately has no `DELETE`: undoing a removal is an operator action
(`scrapev3 remove --restore`), not something a web request should be able to do.

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
