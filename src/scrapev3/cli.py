"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from .fetch import owner_of as fetch_owner_of
from .settings import Settings
from .survey import read_sites, run_survey, summarize

console = Console()


def _configure_event_loop() -> None:
    """Windows: use the selector loop rather than the default proactor loop.

    curl_cffi (and aiodns/c-ares later) need `add_reader`/`add_writer`, which
    ProactorEventLoop does not implement - it otherwise spawns an extra
    selector thread as a workaround. The selector loop caps at 512 file
    descriptors, which is far above our global concurrency of ~32.

    This makes Windows usable, but production should still run under WSL2:
    the 512-FD ceiling is a real limit if concurrency ever grows.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _configure_console() -> None:
    """Force UTF-8 on the Windows console.

    The default codepage is cp1252, which cannot encode the characters rich
    uses for table borders and for its truncation ellipsis - so a table that
    overflows the terminal renders cells as replacement characters and looks
    like corrupted data rather than a narrow window.
    """
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:                               # noqa: BLE001
                pass                                        # not a real console


def _cmd_survey(args: argparse.Namespace) -> int:
    settings = Settings.load()
    sites_path = Path(args.sites)
    if not sites_path.is_file():
        console.print(f"[red]No such file:[/red] {sites_path}")
        return 2

    sites = read_sites(sites_path)
    if args.limit:
        sites = sites[: args.limit]
    if not sites:
        console.print("[red]No usable URLs found in that file.[/red]")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else settings.data_dir / "surveys" / f"survey-{stamp}.jsonl"

    console.print(f"Surveying [bold]{len(sites)}[/bold] domains -> [dim]{out}[/dim]")
    console.print(
        f"[dim]Identity: {settings.identity.user_agent}[/dim]\n"
        f"[dim]Politeness: {settings.politeness.default_delay_s}s/host, "
        f"concurrency {settings.politeness.max_concurrency_per_host}/host, "
        f"{args.concurrency} hosts in parallel[/dim]\n"
    )

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("probing", total=len(sites))

        def tick(result) -> None:
            progress.advance(task)
            progress.update(task, description=f"probing [dim]{result.domain[:40]}[/dim]")

        results = asyncio.run(
            run_survey(sites, out, settings, concurrency=args.concurrency, progress=tick)
        )

    summary = summarize(results)
    summary_path = out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    _print_summary(summary)
    console.print(f"\nRows:    [dim]{out}[/dim]")
    console.print(f"Summary: [dim]{summary_path}[/dim]")
    return 0


def _print_summary(s: dict) -> None:
    n = s["domains_sampled"]
    console.print(f"\n[bold]Survey of {n} domains[/bold]\n")

    reach = Table(title="Reachability", show_header=True, header_style="bold")
    reach.add_column("Metric")
    reach.add_column("%", justify="right")
    reach.add_row("Reachable (HTTP 200)", f"{s['reachable']}")
    reach.add_row("Bot walls (Cloudflare etc.)", f"{s['cloudflare_walls']}")
    reach.add_row("robots.txt disallows us", f"{s['robots_disallow']}")
    console.print(reach)

    disc = Table(title="Discovery cascade - which mechanism wins", header_style="bold")
    disc.add_column("Mechanism")
    disc.add_column("%", justify="right")
    for label, key in (
        ("Google News sitemap", "news_sitemap"),
        ("Any sitemap", "any_sitemap"),
        ("RSS via autodiscovery", "rss_autodiscovered"),
        ("RSS via path probe", "rss_via_probe"),
        ("  ...with full content:encoded", "rss_full_content"),
        ("CMS JSON API", "cms_api"),
        ("Listing page only (fallback)", "listing_only"),
    ):
        disc.add_row(label, f"{s['discovery'][key]}")
    console.print(disc)

    sd = s["structured_data"]
    tested = sd["article_pages_tested"]
    sdt = Table(title=f"Structured data (on {tested} real article pages)", header_style="bold")
    sdt.add_column("Field")
    sdt.add_column("%", justify="right")
    sdt.add_row("JSON-LD Article/NewsArticle/BlogPosting", f"{sd['jsonld_article_type']}")
    sdt.add_row("  headline", f"{sd['has_headline']}")
    sdt.add_row("  datePublished", f"{sd['has_datepublished']}")
    sdt.add_row("  articleBody", f"{sd['has_articlebody']}")
    console.print(sdt)

    js = s["js_escalation"]
    jst = Table(title="JS escalation - the cost driver", header_style="bold")
    jst.add_column("Metric")
    jst.add_column("Value", justify="right")
    jst.add_row("Needs a browser", f"{js['needs_browser']}%")
    jst.add_row("Has hydration payload (mineable, no browser)", f"{js['has_hydration_payload']}%")
    jst.add_row("Median text chars from plain HTTP", f"{js['median_text_chars']}")
    console.print(jst)

    ip = s["shared_ip"]
    console.print(
        f"\n[bold]Shared IPs:[/bold] {ip['distinct_ips']} distinct addresses, "
        f"{ip['domains_on_shared_ip']}% of domains share one, "
        f"largest cluster {ip['largest_cluster']}."
    )
    if ip["largest_cluster"] >= 5:
        console.print(
            "[yellow]  -> Meaningful concentration. Consider pacing per IP as well as "
            "per domain (Nutch queues on (host, IP) for this reason).[/yellow]"
        )

    wp = s["wordpress"]
    console.print(
        f"[bold]WordPress:[/bold] {wp['detected']}% of domains; "
        f"of those, {wp['wp_json_reachable']}% expose a usable /wp-json REST API."
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Check that the local environment can actually run this."""
    console.print("[bold]scrapev3 environment check[/bold]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    ok = True

    table.add_row("Python", "[green]ok[/green]", sys.version.split()[0])

    for mod, why in (
        ("curl_cffi", "TLS/JA4-correct HTTP client"),
        ("selectolax", "fast DOM parsing"),
        ("trafilatura", "body extraction"),
        ("htmldate", "date extraction"),
        ("protego", "robots.txt"),
        ("tldextract", "eTLD+1"),
    ):
        try:
            __import__(mod)
            table.add_row(mod, "[green]ok[/green]", why)
        except ImportError:
            ok = False
            table.add_row(mod, "[red]missing[/red]", f"{why} - pip install {mod}")

    settings = Settings.load()

    # Only needed for the tns sink, so missing is a warning until it is asked for.
    for mod, why in (("pymysql", "MySQL driver"),
                     ("unidecode", "latin1-safe text for press_release")):
        try:
            __import__(mod)
            table.add_row(mod, "[green]ok[/green]", f"{why} (tns sink)")
        except ImportError:
            if settings.tns_sink_enabled:
                ok = False
            table.add_row(mod, "[red]missing[/red]" if settings.tns_sink_enabled
                          else "[dim]not installed[/dim]",
                          f"{why} - pip install -e .[sink]")

    ua = settings.identity.user_agent
    if "bot" in ua.lower() and "+http" in ua:
        table.add_row("User-Agent", "[green]ok[/green]", ua)
    else:
        ok = False
        table.add_row("User-Agent", "[yellow]weak[/yellow]",
                      f"{ua} - needs the token 'bot' and a +https:// contact URL")

    # MySQL. Only needed for the tns.press_release sink, so a missing server is
    # informational unless SCRAPEV3_SINK actually asks for it.
    required = settings.tns_sink_enabled
    if not settings.mysql.configured:
        table.add_row("MySQL", "[red]not configured[/red]" if required else "[dim]not configured[/dim]",
                      "Set SCRAPEV3_MYSQL_HOST in .env for the tns sink")
        ok = ok and not required
    else:
        detail, status = _check_mysql(settings)
        if status != "ok":
            ok = ok and not required
        table.add_row("MySQL", f"[green]ok[/green]" if status == "ok"
                      else f"[{'red' if required else 'yellow'}]{status}[/]", detail)

    # Ollama is only needed from Phase 5; absence is informational, not fatal.
    try:
        import urllib.request
        with urllib.request.urlopen(f"{settings.ollama.host}/api/tags", timeout=2) as r:
            tags = json.loads(r.read())
        models = [m.get("name", "?") for m in tags.get("models", [])]
        hit = settings.ollama.model in models
        table.add_row(
            "Ollama",
            "[green]ok[/green]" if hit else "[yellow]model missing[/yellow]",
            f"{len(models)} model(s). Want '{settings.ollama.model}'"
            + ("" if hit else f" - run: ollama pull {settings.ollama.model}"),
        )
    except Exception:
        table.add_row("Ollama", "[dim]not running[/dim]",
                      "Only needed from Phase 5 (wrapper induction)")

    # A resolver that cannot answer for a whole TLD is invisible everywhere
    # else: every target under it fails at status 0, and the website reports
    # those publishers as broken. 20 .mil targets sat that way through a full
    # corpus run. Two seconds here would have named it.
    import socket

    probes = ("www.centcom.mil", "www.defense.gov", "www.usda.gov")
    unresolved = []
    for host in probes:
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except Exception:                                   # noqa: BLE001
            unresolved.append(host)
    if not unresolved:
        table.add_row("DNS", "[green]ok[/green]",
                      f"system resolver answered for all {len(probes)} probes")
    elif settings.politeness.doh_url:
        table.add_row("DNS", "[yellow]system resolver failing[/yellow]",
                      f"cannot resolve {', '.join(unresolved)} - "
                      f"working around it with DoH ({settings.politeness.doh_url})")
    else:
        ok = False
        table.add_row("DNS", "[red]broken[/red]",
                      f"cannot resolve {', '.join(unresolved)}. These hosts are "
                      "fine - your resolver is not. Fix the host's DNS, or set "
                      "SCRAPEV3_DOH_URL=https://cloudflare-dns.com/dns-query")

    # A capability that is quietly absent is the failure mode this project
    # exists to eliminate, so `doctor` answers it before a run does.
    if settings.browser_enabled:
        try:
            import nodriver           # noqa: F401
            table.add_row("Browser", "[green]ok[/green]",
                          "nodriver present; challenges "
                          + ("on" if settings.browser_challenges_enabled else "off"))
        except ImportError:
            ok = False
            table.add_row("Browser", "[red]missing[/red]",
                          "SCRAPEV3_BROWSER=on but nodriver is not installed - "
                          "pip install -e .[browser]")
    else:
        table.add_row("Browser", "[dim]off[/dim]",
                      "SCRAPEV3_BROWSER=on to render JS newsrooms")

    # "Re-pinned quarterly" was a comment while the pin sat 26 releases back.
    try:
        from curl_cffi.requests import BrowserType

        available = {b.value for b in BrowserType}
        pinned = settings.identity.impersonate
        if pinned not in available:
            ok = False
            table.add_row("Impersonation", "[red]unsupported[/red]",
                          f"{pinned} is not in this curl_cffi build")
        else:
            newest = max((int(b[len("chrome"):]) for b in available
                          if b.startswith("chrome") and b[len("chrome"):].isdigit()),
                         default=0)
            n = int(pinned[6:]) if pinned.startswith("chrome") and pinned[6:].isdigit() else newest
            stale = newest - n > 12
            table.add_row("Impersonation",
                          "[yellow]stale[/yellow]" if stale else "[green]ok[/green]",
                          f"{pinned}" + (f" - newest available is chrome{newest}"
                                         if stale else ""))
    except Exception:                                       # noqa: BLE001
        pass

    console.print(table)
    return 0 if ok else 1


def _check_mysql(settings: Settings) -> tuple[str, str]:
    """(detail, status) for the doctor table."""
    from .tns import connect

    try:
        conn = connect(settings, settings.mysql.sink_db)
    except Exception as exc:                                # noqa: BLE001
        return f"{settings.mysql.host}:{settings.mysql.port} - {exc}", "unreachable"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = %s", (settings.mysql.sink_db,))
            tables = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()

    # All three matter: press_release is the target, and without agencies and
    # url_grp there is no filename prefix, no lede and no owner.
    missing = sorted({"press_release", "agencies", "url_grp"} - tables)
    if missing:
        return (f"MySQL {version} - {settings.mysql.sink_db} is missing "
                f"{', '.join(missing)}"), "incomplete"
    return f"MySQL {version} - {settings.mysql.sink_db}.press_release ready", "ok"


def _cmd_frontier_seed(args: argparse.Namespace) -> int:
    """Load the target list into the frontier."""
    import csv

    from .frontier import open_frontier
    from .urls import canonical_url, registrable_domain

    Settings.load()
    path = Path(args.sites)
    if not path.is_file():
        console.print(f"[red]No such file:[/red] {path}")
        return 2

    rows: list[tuple[int, str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames and "newsroom_url" in reader.fieldnames:
            for r in reader:
                url = canonical_url(r["newsroom_url"])
                dom = r.get("domain") or registrable_domain(url)
                if url and dom:
                    rows.append((int(r.get("a_id") or 0), url, dom))
        else:
            fh.seek(0)
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                url = canonical_url(line.split(",")[0])
                dom = registrable_domain(url)
                if url and dom:
                    rows.append((0, url, dom))

    # Seeding upserts every row of the source CSV, so without this a removed
    # agency comes straight back on the next seed - which would make the whole
    # removal path decorative. The tombstone list is the durable record; the
    # CSV is derived from v2's export and is regenerated, so editing it would
    # not survive either.
    settings = Settings.load()

    # Requested sites are seeded from the same pass as the CSV, because they are
    # the same kind of thing: a target somebody wants crawled. They go in first
    # so the removal filter below applies to them too - a request must not be a
    # way around the tombstone list.
    requested = 0
    if settings.requests_enabled:
        from . import site_requests
        from .urls import canonical_url as _canon, registrable_domain as _reg

        have = {r[1] for r in rows}
        for a_id, raw in site_requests.pending(settings):
            url = _canon(raw)
            dom = _reg(url) if url else ""
            if url and dom and url not in have:
                rows.append((a_id, url, dom))
                have.add(url)
                requested += 1

    skipped = 0
    if settings.removal_enabled:
        removed = _removed_agency_ids(settings)
        if removed:
            before = len(rows)
            rows = [r for r in rows if r[0] not in removed]
            skipped = before - len(rows)

    store = open_frontier()
    try:
        n = store.upsert_sites(rows)
        stats = store.stats()
    finally:
        store.close()

    console.print(f"Seeded [bold]{n}[/bold] targets ({len({d for _, _, d in rows})} distinct domains)")
    if requested:
        console.print(f"[dim]{requested} of them came from the shared request list[/dim]")
    if skipped:
        console.print(f"[dim]Skipped {skipped} target(s) whose agency has been removed[/dim]")
    _print_frontier_stats(stats)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Compose per-agency health, and optionally publish it for the website."""
    from . import status as status_mod
    from .frontier import open_frontier
    from .sink import Sink

    settings = Settings.load()
    frontier = open_frontier()
    sink = Sink(settings.data_dir)
    try:
        rows = status_mod.compose(frontier, sink)
    finally:
        sink.close()
        frontier.close()

    if args.severity:
        rows = [r for r in rows if r.severity == args.severity]
    if args.health:
        rows = [r for r in rows if r.health == args.health]
    if args.domain:
        rows = [r for r in rows if r.domain == args.domain]
    # Not a health word: an agency can be perfectly healthy and still have a
    # newsroom the cascade has never solved, and that is exactly the list worth
    # looking at when deciding what needs work.
    if args.uncached:
        rows = [r for r in rows if r.targets_cached < r.targets]
    if args.due:
        now = datetime.utcnow()
        rows = [r for r in rows
                if r.enabled and (r.next_due_at is None or r.next_due_at <= now)]

    if args.json is not None:
        payload = status_mod.to_json(rows)
        if args.json == "-":
            console.print_json(payload)
        else:
            Path(args.json).write_text(payload, encoding="utf-8")
            console.print(f"Wrote {len(rows)} agencies to {args.json}")
        return 0

    if args.html is not None:
        path = Path(args.html)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(status_mod.to_html(rows), encoding="utf-8")
        console.print(f"Wrote {len(rows)} agencies to {path}")
        # Self-contained on purpose: no server to start, and file:// works.
        console.print(f"[dim]Open it directly: {path.resolve().as_uri()}[/dim]")
        return 0

    if args.publish:
        return _publish_status(settings, rows, prune=not (
            args.severity or args.health or args.domain))

    _print_status(rows, status_mod.summarise(rows), limit=args.limit)
    return 0


def _publish_status(settings: Settings, rows, *, prune: bool) -> int:
    """Push the grid to MySQL for the website to read."""
    from . import status as status_mod

    try:
        conn = status_mod.connect(settings)
    except Exception as exc:                                # noqa: BLE001
        console.print(f"[red]Cannot reach the status table:[/red] {exc}")
        console.print("[dim]It lives in MySQL - set SCRAPEV3_MYSQL_HOST in .env[/dim]")
        return 2
    try:
        status_mod.ensure_table(conn)
        written = status_mod.publish(conn, rows)
        # Only when publishing the whole grid. Pruning against a filtered set
        # would delete every agency the filter excluded.
        dropped = status_mod.prune(conn, [r.a_id for r in rows]) if prune else 0
    finally:
        conn.close()

    console.print(f"Published {written} agencies to "
                  f"{settings.mysql.state_db}.agency_status.")
    if dropped:
        console.print(f"[dim]Removed {dropped} row(s) for agencies no longer "
                      f"in the frontier.[/dim]")
    return 0


_SEVERITY_STYLE = {"ok": "green", "warn": "yellow", "error": "red"}

# `policy` is dim on purpose: it is counted, attributed, and not a to-do.
_OWNER_STYLE = {"us": "red", "site": "yellow", "policy": "dim"}
_BAND_STYLE = {"urgent": "red", "notable": "yellow", "minor": "dim"}


def _print_status(rows, summary: dict, *, limit: int) -> None:
    from .status import RECENT_DAYS, severity_of

    if not rows:
        console.print("[dim]No agencies match.[/dim]")
        return
    parts = [f"[{_SEVERITY_STYLE.get(severity_of(h), 'white')}]{h} {n}[/]"
             for h, n in sorted(summary.items(), key=lambda kv: -kv[1])]
    console.print("  ".join(parts))

    t = Table(header_style="bold")
    t.add_column("a_id", justify="right")
    t.add_column("Domain", overflow="fold")
    t.add_column("Health")
    t.add_column("Source")
    t.add_column("Articles", justify="right")
    t.add_column(f"Last {RECENT_DAYS}d", justify="right")
    t.add_column("Pulled")
    t.add_column("Next due")
    t.add_column("Why", overflow="fold")
    for r in rows[:limit]:
        style = _SEVERITY_STYLE.get(r.severity, "white")
        t.add_row(str(r.a_id), r.domain, f"[{style}]{r.health}[/{style}]",
                  _source(r), str(r.articles), str(r.articles_recent),
                  _ago(r.last_stored_at), _ago(r.next_due_at),
                  r.reason or "")
    console.print(t)
    if len(rows) > limit:
        console.print(f"[dim]{len(rows) - limit} more; raise --limit or filter "
                      f"with --severity / --health / --uncached.[/dim]")


def _source(row) -> str:
    """The cached discovery method, and whether it covers the whole agency.

    An agency with three newsrooms where one is solved is not solved, and
    printing just the winning method would say it was.
    """
    if not row.discovery_method:
        return "[dim]not solved[/dim]"
    if row.targets_cached < row.targets:
        return f"{row.discovery_method} [dim]({row.targets_cached}/{row.targets})[/dim]"
    return row.discovery_method


def _ago(when) -> str:
    """A date as distance from now, signed, because both directions happen.

    `last_stored_at` is always in the past and `next_due_at` usually is not, and
    "in 4h" reads at a glance where a bare timestamp does not.
    """
    if when is None:
        return "[dim]-[/dim]"
    delta = when - datetime.utcnow()
    seconds = abs(delta.total_seconds())
    if seconds < 3600:
        size = f"{int(seconds // 60)}m"
    elif seconds < 86400:
        size = f"{int(seconds // 3600)}h"
    else:
        size = f"{int(seconds // 86400)}d"
    return f"in {size}" if delta.total_seconds() > 0 else f"{size} ago"


def _cmd_faults(args: argparse.Namespace) -> int:
    """What went wrong, ranked by what is worth fixing first."""
    from .faults import FaultStore, summarise, tally, to_json, worst_domains

    settings = Settings.load()
    store = FaultStore(settings.data_dir)
    try:
        ids = store.run_ids(limit=max(1, args.runs))
        if not ids:
            console.print("[dim]No runs recorded yet.[/dim] "
                          "[dim]Faults are written by `scrapev3 crawl`.[/dim]")
            return 0
        rows = store.rows(kind=args.kind, domain=args.domain,
                          owner=args.owner, runs=max(1, args.runs))
        run = store.run(ids[0]) or {}
    finally:
        store.close()

    if args.json is not None:
        payload = to_json(rows, run)
        if args.json == "-":
            console.print_json(payload)
        else:
            Path(args.json).write_text(payload, encoding="utf-8")
            console.print(f"Wrote {len(rows)} fault rows to {args.json}")
        return 0

    # `policy` is hidden by default and counted in the header regardless. A
    # robots refusal is not a defect, and a list that opens with 27 of them is
    # a list nobody reads to the bottom.
    shown = rows if (args.all or args.owner) else [
        r for r in rows if r.owner != "policy"]

    summary = summarise(rows)
    scope = f" ({run.get('scope')})" if run.get("scope") else ""
    console.print(
        f"[bold]{run.get('run_id', ids[0])}[/bold]{scope} - "
        f"{run.get('domains', 0)} domains, {run.get('targets', 0)} targets"
        + (f", last {args.runs} runs" if args.runs > 1 else ""))
    owner = summary["owner"]
    console.print(
        f"[red]us {owner.get('us', 0)}[/red] · "
        f"[yellow]site {owner.get('site', 0)}[/yellow] · "
        f"[dim]policy {owner.get('policy', 0)}[/dim]"
        f"   [dim]{summary['occurrences']} occurrences across "
        f"{summary['domains']} domains[/dim]")

    if not shown:
        console.print("\n[green]Nothing that is ours to fix.[/green]"
                      if rows else "\n[green]No faults recorded.[/green]")
        return 0

    t = Table(title="Needs attention", header_style="bold")
    for col in ("Kind", "Whose", "Sev"):
        t.add_column(col)
    for col in ("Domains", "Articles", "Score"):
        t.add_column(col, justify="right")
    t.add_column("Band")
    t.add_column("Example", overflow="fold")
    for entry in tally(shown)[:args.limit]:
        style = _BAND_STYLE.get(entry.band, "white")
        t.add_row(entry.kind,
                  f"[{_OWNER_STYLE.get(entry.owner, 'white')}]{entry.owner}[/]",
                  str(entry.severity), str(len(entry.domains)),
                  str(entry.occurrences), f"{entry.score:.0f}",
                  f"[{style}]{entry.band}[/{style}]",
                  entry.domains[0] if entry.domains else "")
    console.print(t)

    # Only when a domain failed in more than one way. A domain with a single
    # kind is already fully described by the table above, and twenty of them
    # tied at the same score is a screen of noise that buries the one site
    # actually falling apart.
    worst = [w for w in worst_domains(shown) if len(w[2]) > 1]
    if worst and not args.domain:
        w = Table(title="Worst domains", header_style="bold")
        w.add_column("Domain", overflow="fold")
        w.add_column("Score", justify="right")
        w.add_column("Failed how", overflow="fold")
        for dom, score, kinds in worst:
            w.add_row(dom, str(score), ", ".join(kinds))
        console.print(w)

    if not args.all and not args.owner and len(rows) != len(shown):
        console.print(f"[dim]{len(rows) - len(shown)} row(s) hidden: robots "
                      f"refusals and bot walls are not ours to fix. "
                      f"--all shows them.[/dim]")
    return 0


def _removed_agency_ids(settings: Settings) -> set[int]:
    """The removal list, or an empty set if it cannot be reached.

    Seeding must not fail because the list is unavailable; it just cannot
    filter, and the next crawl reconciles anyway.
    """
    from . import removal

    try:
        conn = removal.connect(settings)
    except Exception as exc:                                # noqa: BLE001
        console.print(f"[yellow]Removal list unreachable, not filtering:[/yellow] {exc}")
        return set()
    try:
        removal.ensure_table(conn)
        return removal.listed(conn)
    finally:
        conn.close()


def _cmd_remove(args: argparse.Namespace) -> int:
    """Add an agency to the shared removal list, and purge it everywhere."""
    from . import removal
    from .frontier import open_frontier
    from .sink import Sink

    settings = Settings.load()
    try:
        conn = removal.connect(settings)
    except Exception as exc:                                # noqa: BLE001
        console.print(f"[red]Cannot reach the removal list:[/red] {exc}")
        console.print("[dim]It lives in MySQL - set SCRAPEV3_MYSQL_HOST in .env[/dim]")
        return 2

    try:
        removal.ensure_table(conn)

        if args.list:
            entries = removal.rows(conn)
            if not entries:
                console.print("[dim]No agencies have been removed.[/dim]")
                return 0
            t = Table(title="Removed agencies", header_style="bold")
            t.add_column("a_id", justify="right")
            t.add_column("Removed at")
            t.add_column("Note", overflow="fold")
            for a_id, when, note in entries:
                t.add_row(str(a_id), str(when), note or "")
            console.print(t)
            return 0

        if args.restore is not None:
            dropped = removal.drop(conn, args.restore)
            console.print(
                f"Took a_id {args.restore} off the removal list ({dropped} row)."
                if dropped else
                f"[yellow]a_id {args.restore} was not on the list.[/yellow]")
            console.print("[dim]Nothing already deleted is restored. Re-seed to "
                          "put the targets back.[/dim]")
            return 0

        if args.a_id is not None:
            removal.add(conn, args.a_id, args.note)
        targets = removal.listed(conn) if args.apply else (
            {args.a_id} if args.a_id is not None else set())
        if not targets:
            console.print("[red]Nothing to do.[/red] "
                          "[dim]Give --a-id, or --apply to reconcile the whole list[/dim]")
            return 2
    finally:
        conn.close()

    frontier = open_frontier()
    sink = Sink(settings.data_dir)
    tns = None
    if settings.tns_sink_enabled:
        from .tns import open_tns_sink
        try:
            tns = open_tns_sink(settings)
        except Exception as exc:                            # noqa: BLE001
            console.print(f"[yellow]press_release not reachable, skipping it:[/yellow] {exc}")

    try:
        reports = removal.reconcile(targets, frontier=frontier, sink=sink, tns=tns)
    finally:
        sink.close()
        frontier.close()
        if tns is not None:
            tns.close()

    _print_removals(reports, targets)
    return 0


def _print_removals(reports, requested: set[int]) -> int:
    if not reports:
        console.print(f"[dim]Nothing left to remove for "
                      f"{', '.join(str(a) for a in sorted(requested))}.[/dim]")
        return 0
    t = Table(title="Removed", header_style="bold")
    t.add_column("a_id", justify="right")
    for col in ("Targets", "Domains", "Indexed", "Archived", "press_release"):
        t.add_column(col, justify="right")
    for r in reports:
        t.add_row(str(r.a_id), str(r.targets), str(r.domains), str(r.indexed),
                  str(r.archived), str(r.press_releases))
    console.print(t)
    for r in reports:
        for err in r.errors:
            console.print(f"[red]a_id {r.a_id}:[/red] {err}")
    console.print("[dim]The JSONL archive was rewritten; that part cannot be "
                  "undone.[/dim]")
    return 0


def _cmd_request(args: argparse.Namespace) -> int:
    """Add a site to the shared request list, and seed it into the frontier."""
    from . import removal, site_requests
    from .frontier import open_frontier

    settings = Settings.load()
    try:
        conn = site_requests.connect(settings)
    except Exception as exc:                                # noqa: BLE001
        console.print(f"[red]Cannot reach the request list:[/red] {exc}")
        console.print("[dim]It lives in MySQL - set SCRAPEV3_MYSQL_HOST in .env[/dim]")
        return 2

    try:
        site_requests.ensure_table(conn)

        if args.list:
            entries = site_requests.rows(conn)
            if not entries:
                console.print("[dim]No sites have been requested.[/dim]")
                return 0
            t = Table(title="Requested sites", header_style="bold")
            t.add_column("a_id", justify="right")
            t.add_column("Newsroom URL", overflow="fold")
            t.add_column("Requested at")
            t.add_column("Note", overflow="fold")
            for a_id, url, when, note in entries:
                t.add_row(str(a_id), url, str(when), note or "")
            console.print(t)
            return 0

        if args.drop:
            if args.a_id is None:
                console.print("[red]--drop needs --a-id.[/red]")
                return 2
            dropped = site_requests.drop(conn, args.a_id, args.url)
            console.print(
                f"Took {dropped} request(s) for a_id {args.a_id} off the list."
                if dropped else
                f"[yellow]a_id {args.a_id} had nothing on the list.[/yellow]")
            console.print("[dim]Anything already seeded stays in the frontier. "
                          "Use `scrapev3 remove` to take it out.[/dim]")
            return 0

        if args.a_id is not None:
            if not args.url:
                console.print("[red]--a-id needs --url.[/red] "
                              "[dim]The frontier's unit is the newsroom URL[/dim]")
                return 2
            site_requests.add(conn, args.a_id, args.url, args.note)

        if args.apply:
            wanted = site_requests.listed(conn)
        elif args.a_id is not None:
            wanted = [(args.a_id, args.url)]
        else:
            console.print("[red]Nothing to do.[/red] [dim]Give --a-id and --url, "
                          "or --apply to reconcile the whole list[/dim]")
            return 2

        # Read inside the same connection: the removal list is the authority on
        # what must not come back, and checking it here is what stops a request
        # from undoing a removal the website also asked for.
        removal.ensure_table(conn)
        removed = removal.listed(conn)
    finally:
        conn.close()

    frontier = open_frontier()
    try:
        report = site_requests.reconcile(wanted, frontier=frontier, removed=removed)
        stats = frontier.stats()
    finally:
        frontier.close()

    _print_requests(report)
    _print_frontier_stats(stats)
    return 0


def _print_requests(report) -> None:
    console.print(f"Seeded [bold]{report.seeded}[/bold] requested target(s)")
    if report.refused:
        console.print(f"[yellow]Refused {len(report.refused)}[/yellow] whose agency "
                      f"is on the removal list: "
                      f"{', '.join(str(a) for a in sorted(set(report.refused)))}")
        console.print("[dim]A removal outranks a request. Use "
                      "`scrapev3 remove --restore A_ID` first if that is wrong.[/dim]")
    for url in report.invalid:
        console.print(f"[red]No usable domain:[/red] {url}")


def _print_frontier_stats(s) -> None:
    table = Table(title="Frontier", header_style="bold")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    table.add_row("Domains (lease/pacing unit)", f"{s.total:,}")
    table.add_row("Newsroom URLs (crawl unit)", f"{s.targets:,}")
    table.add_row("Domains enabled", f"{s.enabled:,}")
    table.add_row("Due now", f"{s.due:,}")
    table.add_row("Currently leased", f"{s.leased:,}")
    table.add_row("Failing (consec > 0)", f"{s.failing:,}")
    table.add_row("Never crawled", f"{s.never_crawled:,}")
    table.add_row("Flagged needs-browser", f"{s.needs_browser:,}")
    console.print(table)


def _cmd_frontier_stats(args: argparse.Namespace) -> int:
    from .frontier import open_frontier

    Settings.load()
    store = open_frontier()
    try:
        reclaimed = store.release_expired_leases()
        stats = store.stats()
        scheduled = store.schedule_enabled
    finally:
        store.close()
    if reclaimed:
        console.print(f"[yellow]Reclaimed {reclaimed} expired lease(s).[/yellow]")
    _print_frontier_stats(stats)
    if not scheduled:
        console.print(
            "[dim]Schedule off (SCRAPEV3_SCHEDULE=off): every enabled domain is due, "
            "and crawling one leaves it due. Lease and per-host pacing unaffected.[/dim]")
    return 0


def _cmd_crawl(args: argparse.Namespace) -> int:
    """Lease domains, discover articles, extract, and store them."""
    debug = getattr(args, "debug", False)
    if debug:
        from .tracing import enable as enable_tracing

        # The same Console the progress bar uses, so rich keeps the bar on the
        # bottom row and prints each decision above it instead of into it.
        enable_tracing(console)

    from .crawl import crawl_once
    from .frontier import open_frontier
    from .sink import Sink

    settings = Settings.load()
    frontier = open_frontier()
    sink = Sink(settings.data_dir)

    # A named scope means "this site, now", not "whatever is due". Resolved
    # here so an unknown a_id fails immediately instead of silently crawling
    # nothing and looking like an extraction failure.
    only_domains = None
    if args.a_id is not None:
        only_domains = frontier.domains_for(a_id=args.a_id)
        if not only_domains:
            console.print(f"[red]No frontier target for a_id {args.a_id}.[/red] "
                          "[dim]Is it in data/sites.csv? Try: scrapev3 seed[/dim]")
            frontier.close()
            sink.close()
            return 2
    elif args.domain:
        only_domains = [args.domain]

    if args.refetch:
        if only_domains is None:
            console.print("[red]--refetch needs a scope[/red] "
                          "[dim](--a-id or --domain), or use: scrapev3 reset --yes[/dim]")
            frontier.close()
            sink.close()
            return 2
        # Without this the dedup check short-circuits before the fetch and the
        # re-crawl stores nothing at all.
        forgotten = sink.forget(domain=args.domain, a_id=args.a_id)
        frontier.make_due(domain=args.domain, a_id=args.a_id)
        console.print(f"[dim]--refetch: forgot {forgotten:,} stored article(s); "
                      "they will be fetched again[/dim]")

    # --sink overrides SCRAPEV3_SINK for this run. Opening the TNS sink up
    # front is deliberate: a bad password or a missing agencies table should
    # stop the run before it has politely spent ten minutes crawling.
    want_tns = args.sink == "tns" if args.sink else settings.tns_sink_enabled
    tns = None
    if want_tns:
        from .tns import open_tns_sink
        try:
            tns = open_tns_sink(settings, dry_run=args.dry_run)
        except Exception as exc:                            # noqa: BLE001
            console.print(f"[red]Cannot open the tns sink:[/red] {exc}")
            frontier.close()
            sink.close()
            return 2

    console.print(
        f"Crawling up to [bold]{args.domains}[/bold] domains, "
        f"max {args.max_articles} articles each, "
        f"window {args.max_age_days}d\n"
        f"[dim]{settings.identity.user_agent} | "
        f"{settings.politeness.default_delay_s}s/host, 1 concurrent/host, "
        f"{settings.politeness.max_concurrency_per_ip} concurrent/IP[/dim]"
    )
    if tns is not None:
        console.print(
            f"[dim]Sink: {settings.mysql.sink_db}.press_release on "
            f"{settings.mysql.host} | {len(tns.agencies):,} agencies loaded"
            + (" | DRY RUN, nothing will be written[/dim]" if args.dry_run else "[/dim]"))
    else:
        console.print("[dim]Sink: JSONL only (--sink tns to load press_release)[/dim]")
    console.print()

    run = dict(
        settings=settings, frontier=frontier, sink=sink, tns=tns,
        domains=args.domains, only_domains=only_domains,
        only_a_id=args.a_id, max_articles=args.max_articles,
        max_age_days=args.max_age_days, concurrency=args.concurrency,
    )

    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(
                "crawling", total=len(only_domains) if only_domains else args.domains)

            def tick(domain: str) -> None:
                progress.advance(task)
                progress.update(task, description=f"crawling [dim]{domain[:38]}[/dim]")

            stats = asyncio.run(crawl_once(progress=tick, **run))
        out_path = sink.path
        sink_stats = sink.stats()
    finally:
        sink.close()
        frontier.close()
        if tns is not None:
            tns.close()

    table = Table(title="Crawl result", header_style="bold")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for label, value in (
        ("Domains leased", stats.domains),
        ("Newsroom URLs crawled", stats.targets),
        ("Articles discovered", stats.discovered),
        ("  already seen (skipped before fetch)", stats.already_seen),
        ("  pages fetched", stats.fetched),
        ("  [green]stored[/green]", stats.stored),
        ("  unusable (short/no date)", stats.unusable),
        ("  older than the window", stats.too_old),
        ("  off-domain (wrong publisher)", stats.off_domain),
        ("  non-news (events/staff/courses)", stats.non_news),
        ("  robots.txt disallowed", stats.robots_disallowed),
        ("  needed a browser", stats.needs_browser),
        ("  body text dupes", stats.body_text_dupes),
        ("  failed", stats.failed),
    ):
        table.add_row(label, f"{value:,}")
    console.print(table)

    if stats.by_method:
        m = Table(title="Discovery method used", header_style="bold")
        m.add_column("Method")
        m.add_column("Domains", justify="right")
        for k, v in sorted(stats.by_method.items(), key=lambda kv: -kv[1]):
            m.add_row(k, str(v))
        console.print(m)

    if stats.by_failure:
        from .faults import attention, band

        f = Table(title="Why fetches failed", header_style="bold")
        f.add_column("Kind")
        f.add_column("Whose")
        f.add_column("Articles", justify="right")
        f.add_column("Domains", justify="right")
        f.add_column("Where", overflow="fold")
        # Ranked, not counted. One site failing forty times and twenty sites
        # failing once are the same total and completely different problems.
        ranked = sorted(
            stats.by_failure.items(),
            key=lambda kv: (-attention(kv[0], len(stats.failure_domains.get(kv[0], ()))),
                            -kv[1]))
        for k, v in ranked:
            doms = sorted(stats.failure_domains.get(k, ()))
            shown = ", ".join(doms[:3]) + (f" +{len(doms) - 3}" if len(doms) > 3 else "")
            style = _OWNER_STYLE.get(fetch_owner_of(k), "white")
            f.add_row(k, f"[{style}]{fetch_owner_of(k)}[/{style}]",
                      str(v), str(len(doms)), shown)
        console.print(f)
        sample = stats.failure_sample.get(ranked[0][0]) if ranked else None
        if sample:
            console.print(f"[dim]  e.g. {ranked[0][0]}: {sample[:100]}[/dim]")

    if stats.by_unusable:
        u = Table(title="Why articles were unusable", header_style="bold")
        u.add_column("Reason", overflow="fold")
        u.add_column("Articles", justify="right")
        u.add_column("Domains", overflow="fold")
        for k, v in sorted(stats.by_unusable.items(), key=lambda kv: -kv[1]):
            doms = sorted(stats.unusable_domains.get(k, ()))
            shown = ", ".join(doms[:3]) + (f" +{len(doms) - 3}" if len(doms) > 3 else "")
            u.add_row(k, str(v), shown)
        console.print(u)

    if stats.by_body_source:
        b = Table(title="Where the body came from", header_style="bold")
        b.add_column("Source")
        b.add_column("Articles", justify="right")
        for k, v in sorted(stats.by_body_source.items(), key=lambda kv: -kv[1]):
            b.add_row(k, str(v))
        console.print(b)

    if tns is not None:
        _print_tns_stats(tns.stats)
        if stats.tns_failed:
            console.print(
                f"[yellow]{stats.tns_failed} article(s) failed to load and are marked "
                "retryable. Re-run: scrapev3 tns backfill[/yellow]")

    if stats.errors:
        console.print(f"\n[yellow]{len(stats.errors)} error(s), first few:[/yellow]")
        for err in stats.errors[:8]:
            console.print(f"  [dim]{err[:110]}[/dim]")

    # The run's failures outlive it now. This line is the whole point of that:
    # the tables above are capped at three domains and eight errors, and used to
    # be the only record there was.
    if stats.faults and Settings.load().faults_enabled:
        console.print("[dim]Recorded. Rank them with: scrapev3 faults[/dim]")

    # Not errors: the cascade declining a site-wide source because it could not
    # be tied to the target's own section. Printed apart from the error list so
    # a healthy run stops looking like a broken one - nine "errors" on a run
    # with nothing wrong is how an error list stops being read at all.
    if stats.notes:
        console.print(f"\n[dim]{len(stats.notes)} note(s) - sources declined "
                      f"for the right reasons:[/dim]")
        for note in stats.notes[:6]:
            console.print(f"  [dim]{note[:110]}[/dim]")

    console.print(
        f"\nTotal stored to date: [bold]{sink_stats['articles']:,}[/bold] articles "
        f"across {sink_stats['domains']:,} domains"
    )
    console.print(f"Output: [dim]{out_path}[/dim]")
    console.print("[dim]Inspect with: scrapev3 show[/dim]")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Print recently scraped articles so results are inspectable."""
    settings = Settings.load()
    files = sorted((settings.data_dir / "articles").glob("articles-*.jsonl"))
    if not files:
        console.print("[yellow]No articles yet.[/yellow] Run: scrapev3 crawl")
        return 1

    records = []
    for path in files:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    if args.domain:
        records = [r for r in records if args.domain in (r.get("domain") or "")]
    records = records[-args.limit:]

    if not records:
        console.print("[yellow]Nothing matched.[/yellow]")
        return 1

    if args.full:
        for r in records:
            console.print(f"\n[bold cyan]{r.get('headline') or '(no headline)'}[/bold cyan]")
            console.print(
                f"[dim]{r.get('domain')} | {r.get('published_at') or 'no date'} "
                f"(via {r.get('date_source')}) | body via {r.get('body_source')} "
                f"| {len(r.get('body') or '')} chars[/dim]")
            console.print(f"[dim]{r.get('url')}[/dim]")
            body = (r.get("body") or "")[: args.chars]
            console.print(body + ("..." if len(r.get("body") or "") > args.chars else ""))
            if r.get("warnings"):
                console.print(f"[yellow]warnings: {', '.join(r['warnings'])}[/yellow]")
    else:
        t = Table(header_style="bold", show_lines=False)
        t.add_column("Date", width=10)
        t.add_column("Domain", width=22, overflow="ellipsis")
        t.add_column("Headline", overflow="ellipsis")
        t.add_column("Body", justify="right", width=7)
        t.add_column("Via", width=12)
        for r in records:
            date = (r.get("published_at") or "")[:10] or "-"
            t.add_row(date, r.get("domain") or "-", r.get("headline") or "(none)",
                      f"{len(r.get('body') or ''):,}", r.get("body_source") or "-")
        console.print(t)

    console.print(f"\n[dim]{len(records)} shown. Files: {settings.data_dir / 'articles'}[/dim]")
    return 0


# ---------------------------------------------------------------------------
# tns - the MySQL output contract
# ---------------------------------------------------------------------------

def _frontier_a_ids() -> list[int]:
    """Every agency id the frontier will actually crawl."""
    from .frontier import open_frontier

    store = open_frontier()
    try:
        return store.agency_ids()
    finally:
        store.close()


def _load_audit(path: Path) -> list:
    """Rebuild audits from a saved run and re-apply the current scoring.

    Each row stores the measurements, not just the conclusion, so a change to
    the rules can be tested against every target already audited without
    spending a single request.
    """
    from .audit import TargetAudit, judge

    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        d.pop("findings", None)
        d.pop("score", None)
        d.pop("verdict", None)
        a = TargetAudit(**d)
        judge(a)
        out.append(a)
    return out


def _cmd_audit_rescore(args: argparse.Namespace) -> int:
    from .audit import summarize

    path = Path(args.rescore)
    if not path.is_file():
        console.print(f"[red]No such file:[/red] {path}")
        return 2
    results = _load_audit(path)
    if not results:
        console.print("[yellow]That file has no rows.[/yellow]")
        return 1
    console.print(f"Re-scored [bold]{len(results)}[/bold] target(s) from "
                  f"[dim]{path}[/dim] with the current rules\n")
    _print_audit(results, summarize(results), path)
    return 0


_VERDICT_STYLE = {"broken": "red", "suspicious": "yellow", "check": "cyan", "ok": "green"}


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run discovery across many targets and rank what looks wrong.

    Read-only: no article pages are fetched, nothing is written to the dedup
    index or to MySQL. It costs the newsroom page plus whatever the cascade
    spends, at the usual per-host pacing.
    """
    import random

    from .audit import audit_target, summarize
    from .fetch import PoliteFetcher
    from .frontier import open_frontier

    if args.rescore:
        return _cmd_audit_rescore(args)

    settings = Settings.load()
    store = open_frontier()
    try:
        if args.a_id is not None:
            rows = [(t.a_id, t.domain, t.newsroom_url, t.discovery_method,
                     t.feed_url, t.feed_absent)
                    for d in store.domains_for(a_id=args.a_id)
                    for t in store.targets_for(d) if t.a_id == args.a_id]
        elif args.domain:
            rows = [(t.a_id, t.domain, t.newsroom_url, t.discovery_method,
                     t.feed_url, t.feed_absent)
                    for t in store.targets_for(args.domain)]
        else:
            raw = store._execute(
                "SELECT a_id, domain, newsroom_url, discovery_method, feed_url, "
                "feed_absent FROM target WHERE enabled = 1")
            rows = [(int(r[0]), r[1], r[2], r[3], r[4], bool(r[5])) for r in raw]
            # One target per domain: auditing 417 house.gov pages tells you the
            # same thing 417 times and spends 417 requests on one origin.
            by_domain = {}
            for r in rows:
                by_domain.setdefault(r[1], r)
            rows = list(by_domain.values())
            random.seed(args.seed)
            random.shuffle(rows)
            rows = rows[: args.limit]
    finally:
        store.close()

    if not rows:
        console.print("[red]No targets matched.[/red] Have you run: scrapev3 seed ?")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else settings.data_dir / "audits" / f"audit-{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    # A full-corpus run is hours long. Resuming matters more than tidiness:
    # losing three hours to a dropped connection is how a measurement stops
    # getting taken.
    if args.resume and out.is_file():
        done = {json.loads(l)["domain"]
                for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}
        rows = [r for r in rows if r[1] not in done]
        console.print(f"[dim]Resuming: {len(done):,} already audited, "
                      f"{len(rows):,} to go[/dim]")
        if not rows:
            console.print("[green]Nothing left to audit.[/green] Re-score with: "
                          f"scrapev3 audit --rescore {out}")
            return 0

    console.print(
        f"Auditing discovery on [bold]{len(rows)}[/bold] target(s) -> [dim]{out}[/dim]")
    console.print(
        f"[dim]No article pages fetched. Nothing written to the index or MySQL. "
        f"{settings.politeness.default_delay_s}s/host, "
        f"{args.concurrency} targets in parallel[/dim]\n")

    async def run():
        sem = asyncio.Semaphore(args.concurrency)
        results = []
        with out.open("a" if args.resume else "w", encoding="utf-8") as fh, Progress(
            TextColumn("[progress.description]{task.description}"), BarColumn(),
            TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("auditing", total=len(rows))

            async with PoliteFetcher(settings) as fetcher:
                async def one(row):
                    a_id, domain, url, method, feed, absent = row
                    async with sem:
                        try:
                            r = await audit_target(
                                fetcher, a_id=a_id, domain=domain, newsroom_url=url,
                                known_method=method, known_feed=feed,
                                feed_absent=bool(absent), limit=args.articles,
                                extract=args.extract)
                        except Exception as exc:            # noqa: BLE001
                            progress.advance(task)
                            console.print(
                                f"[red]{domain}: {type(exc).__name__}: {exc}[/red]")
                            return
                        results.append(r)
                        # Flushed per row, so an interrupted audit is still usable.
                        fh.write(json.dumps(r.as_dict(), ensure_ascii=False) + "\n")
                        fh.flush()
                        progress.advance(task)
                        progress.update(
                            task, description=f"auditing [dim]{domain[:34]}[/dim]")

                await asyncio.gather(*(one(r) for r in rows))
        return results

    results = asyncio.run(run())
    if not results:
        console.print("[red]Nothing completed.[/red]")
        return 1

    summary = summarize(results)
    out.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    _print_audit(results, summary, out)
    return 0


def _audit_tail(results) -> int:
    """Show every broken/suspicious target, but cap the tail so a bad run does
    not bury the summary above it."""
    bad = sum(1 for r in results if r.verdict in ("broken", "suspicious"))
    return max(min(bad, 25), 10)


def _print_audit(results, summary: dict, out: Path) -> None:
    v = Table(title="Verdict", header_style="bold", box=box.SIMPLE)
    v.add_column("Verdict")
    v.add_column("Targets", justify="right")
    v.add_column("Meaning", overflow="fold")
    for name, meaning in (
        ("broken", "nothing found, wrong publisher, or nothing matching the page"),
        ("suspicious", "results do not look like this section's articles"),
        ("check", "worked, but on a weaker footing than you would like"),
        ("ok", "no flags raised"),
    ):
        n = summary["verdicts"].get(name, 0)
        if n:
            v.add_row(f"[{_VERDICT_STYLE[name]}]{name}[/]", f"{n:,}", meaning)
    console.print(v)

    m = Table(title="Which source won", header_style="bold", box=box.SIMPLE)
    m.add_column("Method")
    m.add_column("Targets", justify="right")
    for k, n in sorted(summary["methods"].items(), key=lambda kv: -kv[1]):
        m.add_row(str(k), f"{n:,}")
    console.print(m)

    if summary["findings"]:
        f = Table(title="Flags raised", header_style="bold", box=box.SIMPLE)
        f.add_column("Flag")
        f.add_column("Targets", justify="right")
        for k, n in sorted(summary["findings"].items(), key=lambda kv: -kv[1]):
            f.add_row(k, f"{n:,}")
        console.print(f)

    ov = summary["median_overlap"]
    if ov is not None:
        console.print(f"\n[bold]Median overlap with the newsroom page:[/bold] {ov:.0%}")
    console.print(f"[bold]Clean:[/bold] {summary['pct_clean']}% of targets raised no flag")

    ex = summary.get("extraction") or {}
    if ex.get("probed"):
        e = Table(title="End to end - one article fetched per domain",
                  header_style="bold", box=box.SIMPLE)
        e.add_column("Stage")
        e.add_column("Domains", justify="right")
        e.add_column("% of probed", justify="right")
        pr = ex["probed"]
        for label, key in (("article fetched", "article_fetched"),
                           ("  got a headline", "got_headline"),
                           ("  got a body (300+ chars)", "got_body"),
                           ("  got a date", "got_date"),
                           ("  [green]usable article[/green]", "usable")):
            v = ex.get(key, 0)
            e.add_row(label, f"{v:,}", f"{100 * v / pr:.1f}%")
        console.print(e)

    tlds = summary.get("by_tld") or {}
    if len(tlds) > 1:
        t2 = Table(title="By TLD", header_style="bold", box=box.SIMPLE)
        t2.add_column("TLD")
        t2.add_column("Domains", justify="right")
        t2.add_column("Workable", justify="right")
        t2.add_column("% workable", justify="right")
        for tld, b in list(tlds.items())[:10]:
            t2.add_row(tld, f"{b['targets']:,}", f"{b['workable']:,}", f"{b['pct_workable']}%")
        console.print(t2)

    worst = sorted((r for r in results if r.findings),
                   key=lambda r: (-r.score, r.domain))[: _audit_tail(results)]
    if worst:
        console.print("\n[bold]Worst first - start here[/bold]")
        for r in worst:
            style = _VERDICT_STYLE[r.verdict]
            console.print(
                f"\n  [{style}]{r.verdict.upper():<10}[/] [bold]{r.domain}[/bold] "
                f"[dim]a_id {r.a_id} | via {r.method} | {r.n_articles} result(s)[/dim]")
            console.print(f"  [dim]{r.newsroom_url}[/dim]")
            for fi in r.findings:
                console.print(f"    [{style}]-[/] {fi.detail}")
            for u in r.sample[:2]:
                console.print(f"    [dim]e.g. {u[:94]}[/dim]")

    console.print(f"\nRows:    [dim]{out}[/dim]")
    console.print(f"Summary: [dim]{out.with_suffix('.summary.json')}[/dim]")
    console.print("[dim]Drill into one with: scrapev3 audit --domain <domain>[/dim]")


def _cmd_tns_status(args: argparse.Namespace) -> int:
    """What the sink would write, and what it can't."""
    from .tns import AgencyDirectory, connect

    settings = Settings.load()
    if not settings.mysql.configured:
        console.print("[red]No MySQL host configured.[/red] Set SCRAPEV3_MYSQL_HOST in .env")
        return 2

    conn = connect(settings, settings.mysql.sink_db)
    db = settings.mysql.sink_db
    try:
        directory = AgencyDirectory.load(conn, db=db,
                                         group_filter=settings.tns.group_filter)
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {db}.press_release")
            total = cur.fetchone()[0]
            cur.execute(f"SELECT status, COUNT(*) FROM {db}.press_release "
                        "GROUP BY status ORDER BY 2 DESC")
            by_status = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) FROM {db}.press_release "
                        "WHERE create_date >= CURDATE()")
            today = cur.fetchone()[0]
    finally:
        conn.close()

    t = Table(title=f"{db}.press_release", header_style="bold")
    t.add_column("Metric")
    t.add_column("Count", justify="right")
    t.add_row("Rows", f"{total:,}")
    t.add_row("Loaded today", f"{today:,}")
    for status, n in by_status:
        t.add_row(f"  status {status or '(null)'}", f"{n:,}")
    console.print(t)

    coverage = directory.coverage(_frontier_a_ids())
    c = Table(title="Agency directory coverage", header_style="bold")
    c.add_column("Metric")
    c.add_column("Count", justify="right")
    c.add_row("Agencies in directory", f"{len(directory):,}")
    c.add_row("Frontier target agency ids", f"{coverage['targets']:,}")
    c.add_row("  [green]resolvable[/green]", f"{coverage['known']:,}")
    c.add_row("  [yellow]missing from agencies[/yellow]", f"{coverage['missing']:,}")
    c.add_row("  no prefix or lede", f"{coverage['unusable']:,}")
    c.add_row("  no owner (uname null or -1)", f"{coverage['no_uname']:,}")
    console.print(c)
    if coverage["missing"]:
        console.print(
            f"[yellow]{coverage['missing']} agency id(s) in the frontier have no row in "
            f"{db}.agencies - their articles are scraped but cannot be loaded.[/yellow]\n"
            f"[dim]First few: {coverage['missing_ids']}[/dim]")

    from .sink import Sink
    sink = Sink(settings.data_dir)
    try:
        local = sink.stats()
    finally:
        sink.close()
    s = Table(title="Local articles by load state", header_style="bold")
    s.add_column("State")
    s.add_column("Articles", justify="right")
    for state, n in sorted(local["tns"].items(), key=lambda kv: -kv[1]):
        s.add_row(state, f"{n:,}")
    console.print(s)
    return 0


def _jsonl_records(settings: Settings) -> dict[str, dict]:
    """Every article ever written, keyed by URL. The JSONL is the archive; the
    SQLite index only holds metadata, so a backfill needs the body from here."""
    records: dict[str, dict] = {}
    for path in sorted((settings.data_dir / "articles").glob("articles-*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:                           # noqa: BLE001
                    continue
                if rec.get("url"):
                    records[rec["url"]] = rec
    return records


def _cmd_tns_backfill(args: argparse.Namespace) -> int:
    """Load articles that were scraped but never made it into press_release.

    This is the reason the dedup index carries a load state at all. Without it,
    an article stored while MySQL was down would be marked seen and never
    offered again - v2's fail-closed dedup, which silently dropped articles on
    any database hiccup.
    """
    from .sink import Sink
    from .tns import open_tns_sink

    settings = Settings.load()
    sink = Sink(settings.data_dir)

    if args.resync:
        # The index is a cache of what press_release contains. A TRUNCATE or a
        # restore from a dump invalidates it, and without this the articles are
        # marked loaded against rows that no longer exist.
        from .tns import open_tns_sink as _open
        probe = _open(settings, dry_run=True)
        try:
            loaded = sink.loaded_tns()
            gone = set(probe.missing_filenames([f for _u, f in loaded]))
        finally:
            probe.close()
        stale = [u for u, f in loaded if f in gone]
        if stale:
            sink.reset_tns(stale)
            console.print(f"[yellow]Resync:[/yellow] {len(stale)} article(s) were marked "
                          f"loaded but are no longer in {settings.mysql.sink_db}"
                          ".press_release. They will be offered again.")
        else:
            console.print(f"[dim]Resync: all {len(loaded)} loaded article(s) are still "
                          "present in press_release.[/dim]")

    pending = sink.pending_tns(limit=args.limit)
    if not pending:
        console.print("[green]Nothing pending.[/green] Every stored article has a load state.")
        sink.close()
        return 0

    console.print(f"Backfilling [bold]{len(pending)}[/bold] article(s)"
                  + (" [yellow](dry run - no writes)[/yellow]" if args.dry_run else ""))
    records = _jsonl_records(settings)
    tns = open_tns_sink(settings, dry_run=args.dry_run)
    missing_body = 0
    try:
        for url, _domain, a_id in pending:
            rec = records.get(url)
            if not rec or not rec.get("body") or not rec.get("published_at"):
                missing_body += 1
                continue
            outcome = tns.load(
                a_id=int(rec.get("a_id") or a_id or 0),
                headline=rec.get("headline") or "",
                body=rec["body"],
                published=datetime.fromisoformat(rec["published_at"]),
                url=url,
            )
            if args.dry_run:
                continue
            if outcome == "inserted":
                sink.mark_tns(url, "loaded", tns.last_filename)
            elif outcome == "insert_error":
                sink.mark_tns(url, "error")
            else:
                sink.mark_tns(url, f"rejected:{outcome}")
    finally:
        tns.close()
        sink.close()

    _print_tns_stats(tns.stats)
    if missing_body:
        console.print(f"[yellow]{missing_body} pending article(s) had no body in the "
                      "JSONL archive and were left pending.[/yellow]")
    if args.dry_run and tns.pending:
        console.print("\n[bold]First composed row:[/bold]")
        _print_press_release(tns.pending[0])
    return 0


def _cmd_tns_show(args: argparse.Namespace) -> int:
    """Print rows straight from press_release, so the contract is inspectable."""
    from .tns import connect

    settings = Settings.load()
    db = settings.mysql.sink_db
    conn = connect(settings, db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT pr_id, a_id, content_date, status, uname, location, "
                f"filename, headline, headline2, body_txt FROM {db}.press_release "
                "ORDER BY pr_id DESC LIMIT %s", (args.limit,))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        console.print("[yellow]press_release is empty.[/yellow] Run: scrapev3 crawl --sink tns")
        return 1

    if args.full:
        for r in rows:
            console.print(f"\n[bold cyan]{r[7]}[/bold cyan]")
            console.print(f"[dim]pr_id {r[0]} | a_id {r[1]} | {r[2]} | status {r[3]} "
                          f"| {r[4]} | {r[5] or 'no location'}[/dim]")
            console.print(f"[dim]{r[6]}[/dim]")
            if r[8]:
                console.print(f"[yellow]headline2: {r[8]}[/yellow]")
            body = r[9] or ""
            console.print(body[: args.chars] + ("..." if len(body) > args.chars else ""))
    else:
        # Sized for an 80-column terminal. Body length lives in --full; the
        # filename already carries the headline tail, so the compact view
        # spends its width on what identifies a row.
        # SIMPLE box: full borders cost 21 columns of an 80-column terminal,
        # enough that rich starts squeezing the fixed-width columns to nothing.
        t = Table(header_style="bold", box=box.SIMPLE)
        # min_width, not width: when the table exceeds the terminal, rich
        # shrinks fixed-width columns too, and an id squeezed to nothing is
        # worse than a truncated headline.
        t.add_column("pr_id", justify="right", min_width=5, no_wrap=True)
        t.add_column("a_id", justify="right", min_width=6, no_wrap=True)
        t.add_column("Date", min_width=10, no_wrap=True)
        t.add_column("St", min_width=2, no_wrap=True)
        t.add_column("Filename", min_width=20, max_width=30,
                     overflow="ellipsis", no_wrap=True)
        t.add_column("Headline", min_width=12, overflow="ellipsis", no_wrap=True)
        for r in rows:
            t.add_row(str(r[0]), str(r[1]), str(r[2]), r[3] or "-",
                      r[6] or "-", r[7] or "-")
        console.print(t)
    console.print(f"\n[dim]{len(rows)} row(s) from {db}.press_release[/dim]")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    """Undo a test run so the same sites can be crawled again.

    Three stores have to move together, which is the whole reason this command
    exists. Truncating `press_release` on its own leaves the dedup index still
    remembering every URL - and that check runs before the fetch, so the next
    run stores nothing and looks broken. The frontier meanwhile will not offer
    the domain again until its revisit period elapses.
    """
    from .frontier import open_frontier
    from .sink import Sink

    settings = Settings.load()
    scope = (f"domain {args.domain}" if args.domain
             else f"a_id {args.a_id}" if args.a_id is not None
             else "EVERYTHING")

    if not (args.domain or args.a_id is not None) and not args.yes:
        console.print("[red]Refusing to reset everything without --yes.[/red]\n"
                      "[dim]Scope it with --domain or --a-id, or pass --yes.[/dim]")
        return 2

    sink = Sink(settings.data_dir)
    frontier = open_frontier()
    tns = None
    try:
        # Resolve everything BEFORE deleting anything. Three separate stores
        # cannot share a transaction, so the only protection against a
        # half-applied reset is to do all the work that can fail - opening a
        # MySQL connection, resolving a scope - while nothing has moved yet.
        a_ids: list[int] | None = None
        if args.tns:
            from .tns import open_tns_sink

            if args.a_id is not None:
                a_ids = [args.a_id]
            elif args.domain:
                # `None` means every row, so it is only ever passed when the
                # user asked for that and confirmed - never as the residue of a
                # scope that resolved to nothing.
                a_ids = sorted({t.a_id for t in frontier.targets_for(args.domain)})
                if not a_ids:
                    console.print(f"[yellow]No frontier target for {args.domain}, so no "
                                  "press_release rows could be identified.[/yellow]")
            tns = open_tns_sink(settings)

        forgotten = sink.forget(domain=args.domain, a_id=args.a_id)
        due = frontier.make_due(domain=args.domain, a_id=args.a_id)
        # Kept by default. Re-deriving where a site's articles come from costs
        # a cold cascade - nine feed probes and a sitemap index walk, all at
        # the per-host delay - to arrive at the answer already on file. The
        # usual reason to reset is to re-check the OUTPUT, not the discovery,
        # so --relearn asks for that explicitly.
        relearn = (frontier.forget_discovery(domain=args.domain, a_id=args.a_id)
                   if args.relearn else 0)
        deleted = tns.delete_rows(a_ids) if tns is not None else None
    finally:
        sink.close()
        frontier.close()
        if tns is not None:
            tns.close()

    console.print(f"Reset [bold]{scope}[/bold]")
    console.print(f"  {forgotten:,} article(s) forgotten - they will be fetched again")
    console.print(f"  {due:,} domain(s) due now")
    if relearn:
        console.print(f"  {relearn:,} target(s) will re-run the full discovery cascade")
    else:
        console.print("[dim]  cached discovery source kept (--relearn to re-derive it)[/dim]")
    if deleted is not None:
        console.print(f"  {deleted:,} row(s) deleted from "
                      f"{settings.mysql.sink_db}.press_release")
    elif args.domain or args.a_id is not None:
        console.print("[dim]  press_release untouched (--tns to clear it too)[/dim]")
    console.print("\n[dim]The JSONL archive is append-only and was left alone.[/dim]")
    return 0


def _print_press_release(row) -> None:
    console.print(f"[dim]filename:[/dim] {row.filename}")
    console.print(f"[dim]a_id {row.a_id} | {row.content_date} | status {row.status} "
                  f"| uname {row.uname} | location {row.location} "
                  f"| {row.word_count} words[/dim]")
    console.print(f"[bold cyan]{row.headline}[/bold cyan]")
    if row.headline2:
        console.print(f"[yellow]headline2: {row.headline2}[/yellow]")
    console.print(row.body_txt[:1200] + ("..." if len(row.body_txt) > 1200 else ""))


def _print_tns_stats(stats) -> None:
    t = Table(title="tns.press_release", header_style="bold")
    t.add_column("Outcome")
    t.add_column("Articles", justify="right")
    labels = {
        "inserted": "[green]inserted[/green]",
        "widened": "  of those, filename widened past a collision",
        "short_doc": "  of those, status W (short doc, needs review)",
        "no_uname": "  of those, no owner (uname null or -1)",
        "duplicate": "already loaded",
        "no_agency": "no agency row (a_id not in tns.agencies)",
        "no_lede": "agency has no lede template",
        "no_headline": "no headline",
        "too_short": "under the word floor",
        "too_long": "body exceeds the TEXT column",
        "insert_error": "[red]insert failed (retryable)[/red]",
    }
    for key, label in labels.items():
        value = getattr(stats, key, 0)
        if value:
            t.add_row(label, f"{value:,}")
    console.print(t)
    if stats.errors:
        console.print(f"[yellow]{len(stats.errors)} note(s), first few:[/yellow]")
        for err in stats.errors[:8]:
            console.print(f"  [dim]{err[:120]}[/dim]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scrapev3",
        description="Layout-agnostic news and press-release scraper.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_survey = sub.add_parser(
        "survey",
        help="Phase 1: measure discovery/extraction coverage across a sample of sites.",
    )
    p_survey.add_argument("sites", help="File of newsroom URLs (.txt one-per-line, or .csv)")
    p_survey.add_argument("--limit", type=int, default=None, help="Only probe the first N domains")
    p_survey.add_argument("--out", default=None, help="Output JSONL path")
    p_survey.add_argument("--concurrency", type=int, default=16,
                          help="Domains probed in parallel (per-host concurrency is always 1)")
    p_survey.set_defaults(func=_cmd_survey)

    p_doctor = sub.add_parser("doctor", help="Check the local environment.")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_seed = sub.add_parser("seed", help="Load the target list into the frontier.")
    p_seed.add_argument("sites", nargs="?", default="data/sites.csv",
                        help="sites.csv (a_id,newsroom_url,domain) or a plain URL list")
    p_seed.set_defaults(func=_cmd_frontier_seed)

    p_fstats = sub.add_parser("frontier", help="Show frontier state.")
    p_fstats.set_defaults(func=_cmd_frontier_stats)

    p_crawl = sub.add_parser("crawl", help="Crawl due domains and store articles.")
    p_crawl.add_argument("--domains", type=int, default=10, help="Domains to lease this pass")
    p_crawl.add_argument("--max-articles", type=int, default=10, help="Max articles per newsroom URL")
    p_crawl.add_argument("--max-age-days", type=int, default=30, help="Ignore articles older than this")
    p_crawl.add_argument("--concurrency", type=int, default=8, help="Domains crawled in parallel")
    crawl_scope = p_crawl.add_mutually_exclusive_group()
    crawl_scope.add_argument("--a-id", type=int, default=None,
                             help="Crawl only this agency, ignoring the due queue")
    crawl_scope.add_argument("--domain", default=None,
                             help="Crawl only this registrable domain, ignoring the due queue")
    p_crawl.add_argument("--refetch", action="store_true",
                         help="With --a-id/--domain: forget stored articles first so "
                              "they are fetched again instead of skipped as seen")
    p_crawl.add_argument("--sink", choices=["jsonl", "tns"], default=None,
                         help="Override SCRAPEV3_SINK. 'tns' also loads tns.press_release")
    p_crawl.add_argument("--debug", action="store_true",
                         help="Log every decision: which source won and why, and "
                              "why each discovered article was kept or dropped")
    p_crawl.add_argument("--dry-run", action="store_true",
                         help="With --sink tns: compose the rows but write nothing to MySQL")
    p_crawl.set_defaults(func=_cmd_crawl)

    p_reset = sub.add_parser(
        "reset", help="Undo a test run: forget articles and make domains due again.")
    scope = p_reset.add_mutually_exclusive_group()
    scope.add_argument("--domain", default=None, help="Only this registrable domain")
    scope.add_argument("--a-id", type=int, default=None, help="Only this agency id")
    p_reset.add_argument("--tns", action="store_true",
                         help="Also delete the matching rows from press_release")
    p_reset.add_argument("--relearn", action="store_true",
                         help="Also forget where the articles come from, forcing a "
                              "cold discovery cascade on the next crawl (slow)")
    p_reset.add_argument("--yes", action="store_true",
                         help="Required to reset every domain at once")
    p_reset.set_defaults(func=_cmd_reset)

    p_remove = sub.add_parser(
        "remove",
        help="Remove an agency on request: purge it everywhere and keep it out.")
    p_remove.add_argument("--a-id", type=int, default=None,
                          help="The agency to remove")
    p_remove.add_argument("--note", default=None,
                          help="Why, recorded on the shared list")
    p_remove.add_argument("--list", action="store_true",
                          help="Show the agencies already removed")
    p_remove.add_argument("--apply", action="store_true",
                          help="Reconcile the whole list now, without crawling")
    p_remove.add_argument("--restore", type=int, default=None, metavar="A_ID",
                          help="Take an agency off the list. Does NOT restore "
                               "anything already deleted; re-seed for that")
    p_remove.set_defaults(func=_cmd_remove)

    p_request = sub.add_parser(
        "request",
        help="Add a site on request: seed the shared list into the frontier.")
    p_request.add_argument("--a-id", type=int, default=None,
                           help="The agency the newsroom belongs to")
    p_request.add_argument("--url", default=None,
                           help="The newsroom URL to crawl")
    p_request.add_argument("--note", default=None,
                           help="Why, recorded on the shared list")
    p_request.add_argument("--list", action="store_true",
                           help="Show the sites already requested")
    p_request.add_argument("--apply", action="store_true",
                           help="Reconcile the whole list now, without crawling")
    p_request.add_argument("--drop", action="store_true",
                           help="Take a request off the list (needs --a-id). "
                                "Does NOT un-seed it; use `remove` for that")
    p_request.set_defaults(func=_cmd_request)

    p_faults = sub.add_parser(
        "faults",
        help="What went wrong, ranked by what is worth fixing first.")
    p_faults.add_argument("--owner", choices=["us", "site", "policy"],
                          default=None,
                          help="Only faults of this kind: 'us' is the to-do list")
    p_faults.add_argument("--kind", default=None,
                          help="Only this failure kind (dns, tls, http_4xx, ...)")
    p_faults.add_argument("--domain", default=None, help="Only this domain")
    p_faults.add_argument("--all", action="store_true",
                          help="Include robots refusals and bot walls, which "
                               "are hidden by default")
    p_faults.add_argument("--runs", type=int, default=1,
                          help="Aggregate the last N runs (default 1)")
    p_faults.add_argument("--limit", type=int, default=15,
                          help="Rows to print (default 15)")
    p_faults.add_argument("--json", nargs="?", const="-", default=None,
                          metavar="PATH",
                          help="Emit the same payload as JSON; '-' prints it")
    p_faults.set_defaults(func=_cmd_faults)

    p_status = sub.add_parser(
        "status",
        help="Per-agency health, for the website's grid. Read-only unless --publish.")
    p_status.add_argument("--publish", action="store_true",
                          help="Upsert the grid into MySQL for the website to read")
    p_status.add_argument("--json", nargs="?", const="-", default=None,
                          metavar="PATH",
                          help="Emit the same payload as JSON; '-' prints it. "
                               "Feeds clients/status_demo.php when there is no DB")
    p_status.add_argument("--uncached", action="store_true",
                          help="Only agencies with a newsroom discovery has "
                               "never solved")
    p_status.add_argument("--due", action="store_true",
                          help="Only agencies the schedule will pick up next")
    p_status.add_argument("--html", nargs="?", const="data/status.html",
                          default=None, metavar="PATH",
                          help="Write a standalone page (data embedded, no "
                               "server needed) and print its file:// URL")
    p_status.add_argument("--severity", choices=["ok", "warn", "error"],
                          default=None, help="Only agencies in this band")
    p_status.add_argument("--health", default=None,
                          help="Only this health word (healthy, empty, stale, ...)")
    p_status.add_argument("--domain", default=None, help="Only this domain")
    p_status.add_argument("--limit", type=int, default=40,
                          help="Rows to print (default 40)")
    p_status.set_defaults(func=_cmd_status)

    p_audit = sub.add_parser(
        "audit", help="Run discovery across targets and rank what looks wrong.")
    audit_scope = p_audit.add_mutually_exclusive_group()
    audit_scope.add_argument("--a-id", type=int, default=None, help="Audit one agency")
    audit_scope.add_argument("--domain", default=None, help="Audit one domain")
    p_audit.add_argument("--limit", type=int, default=50,
                         help="Domains to sample when no scope is given")
    p_audit.add_argument("--articles", type=int, default=25,
                         help="Results to ask discovery for per target")
    p_audit.add_argument("--concurrency", type=int, default=8,
                         help="Targets audited in parallel (per-host pacing unchanged)")
    p_audit.add_argument("--seed", type=int, default=20260827,
                         help="Sampling seed, so a run is repeatable")
    p_audit.add_argument("--out", default=None, help="Output JSONL path")
    p_audit.add_argument("--extract", action="store_true",
                         help="Also fetch ONE article per domain and report whether it "
                              "extracts - turns the audit into an end-to-end measure "
                              "for one extra request per domain")
    p_audit.add_argument("--resume", action="store_true",
                         help="Skip domains already in --out and append. Essential "
                              "for a full-corpus run")
    p_audit.add_argument("--rescore", default=None, metavar="FILE",
                         help="Re-apply the current scoring to a saved audit "
                              "JSONL, without fetching anything")
    p_audit.set_defaults(func=_cmd_audit)

    p_tns = sub.add_parser("tns", help="The tns.press_release output contract.")
    tns_sub = p_tns.add_subparsers(dest="tns_command", required=True)

    p_tns_status = tns_sub.add_parser(
        "status", help="Row counts, agency coverage, and what is waiting to load.")
    p_tns_status.set_defaults(func=_cmd_tns_status)

    p_tns_backfill = tns_sub.add_parser(
        "backfill", help="Load stored articles that never reached press_release.")
    p_tns_backfill.add_argument("--limit", type=int, default=None,
                                help="Only attempt the first N pending articles")
    p_tns_backfill.add_argument("--dry-run", action="store_true",
                                help="Compose the rows and report, but write nothing")
    p_tns_backfill.add_argument("--resync", action="store_true",
                                help="First re-check articles marked loaded against "
                                     "press_release, and re-offer any whose row is gone "
                                     "(after a TRUNCATE or a restore)")
    p_tns_backfill.set_defaults(func=_cmd_tns_backfill)

    p_tns_show = tns_sub.add_parser("show", help="Print recent press_release rows.")
    p_tns_show.add_argument("--limit", type=int, default=20)
    p_tns_show.add_argument("--full", action="store_true", help="Print body_txt too")
    p_tns_show.add_argument("--chars", type=int, default=1200)
    p_tns_show.set_defaults(func=_cmd_tns_show)

    p_show = sub.add_parser("show", help="Inspect scraped articles.")
    p_show.add_argument("--limit", type=int, default=20)
    p_show.add_argument("--domain", default=None, help="Filter by domain substring")
    p_show.add_argument("--full", action="store_true", help="Print body text, not just a table")
    p_show.add_argument("--chars", type=int, default=600, help="Body chars to print with --full")
    p_show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    _configure_console()
    _configure_event_loop()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow] Partial results were flushed to disk.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
