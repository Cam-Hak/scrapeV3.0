#!/usr/bin/env python
"""Read-only SQL console for the two local SQLite stores.

There is no sqlite3 CLI on Windows by default, and both stores answer
questions the CLI subcommands do not: `frontier.sqlite` holds what the
crawler learned about each site, `articles.sqlite` holds what it has seen.

Opened read-only, so this can be run against a live crawl without taking a
write lock away from it.

Usage:
    python scripts/sql.py                                  # list tables
    python scripts/sql.py --schema domain_state            # one table's columns
    python scripts/sql.py "SELECT * FROM domain_state LIMIT 5"
    python scripts/sql.py --db articles "SELECT * FROM article LIMIT 5"
    python scripts/sql.py --wide "SELECT * FROM domain_state LIMIT 3"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

DBS = {
    "frontier": "data/frontier.sqlite",
    "articles": "data/articles.sqlite",
}
console = Console()


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        console.print(f"[red]{path} does not exist.[/red] Run `scrapev3 seed` or a crawl first.")
        raise SystemExit(1)
    # Read-only: a running crawl keeps its write lock.
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def show_tables(conn: sqlite3.Connection, label: str) -> None:
    table = Table(title=f"{label} - tables", show_header=True, header_style="bold")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    table.add_column("Columns", overflow="fold")
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        rows = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({name})")]
        table.add_row(name, f"{rows:,}", ", ".join(cols))
    console.print(table)


def show_schema(conn: sqlite3.Connection, name: str) -> None:
    table = Table(title=f"{name}", show_header=True, header_style="bold")
    for col in ("Column", "Type", "Not null", "Default", "PK"):
        table.add_column(col)
    for c in conn.execute(f"PRAGMA table_info({name})"):
        table.add_row(c[1], c[2], "yes" if c[3] else "", str(c[4] or ""), "yes" if c[5] else "")
    console.print(table)


def run_query(conn: sqlite3.Connection, sql: str, wide: bool, width: int) -> None:
    try:
        cur = conn.execute(sql)
    except sqlite3.Error as exc:
        console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
        raise SystemExit(1)
    rows = cur.fetchall()
    if not rows:
        console.print("[dim]0 rows[/dim]")
        return

    if wide:
        # One field per line - sqlite's \G. For rows too wide for a table.
        for i, row in enumerate(rows, 1):
            console.print(f"[bold]--- row {i} ---[/bold]")
            for key in row.keys():
                console.print(f"  [cyan]{key:<18}[/cyan] {row[key]}")
        console.print(f"\n[dim]{len(rows)} row(s)[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    for key in rows[0].keys():
        table.add_column(key, overflow="fold")
    for row in rows:
        table.add_row(*[("" if row[k] is None else str(row[k]))[:width] for k in row.keys()])
    console.print(table)
    console.print(f"[dim]{len(rows)} row(s)[/dim]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sql", nargs="?", help="SQL to run. Omit to list tables.")
    ap.add_argument("--db", choices=sorted(DBS), default="frontier")
    ap.add_argument("--schema", metavar="TABLE", help="Describe one table's columns")
    ap.add_argument("--wide", action="store_true",
                    help="One field per line, for rows too wide for a table")
    ap.add_argument("--width", type=int, default=60, help="Truncate cells to N chars")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    conn = connect(root / DBS[args.db])
    try:
        if args.schema:
            show_schema(conn, args.schema)
        elif args.sql:
            run_query(conn, args.sql, args.wide, args.width)
        else:
            show_tables(conn, DBS[args.db])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
