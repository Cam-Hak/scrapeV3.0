"""The TNS output contract: `tns.press_release` and the agency directory.

`record.py` composes a row and touches no I/O. `agencies.py` reads the
editor-maintained lookup. `sink.py` inserts. Splitting it that way is what lets
the contract be tested without a database, which matters because the contract
is the part that must not drift.
"""

from __future__ import annotations

from typing import Any

from ..settings import Settings
from .agencies import Agency, AgencyDirectory
from .record import (PressRelease, Rejected, build_press_release, clean_for_tns,
                     compose_body, dateline_location, format_lede,
                     normalise_body, tns_filename, to_ascii)
from .sink import TnsSink, TnsStats

__all__ = [
    "Agency", "AgencyDirectory", "PressRelease", "Rejected", "TnsSink",
    "TnsStats", "build_press_release", "clean_for_tns", "compose_body",
    "connect", "dateline_location", "format_lede", "normalise_body",
    "open_tns_sink", "tns_filename", "to_ascii",
]


def connect(settings: Settings, database: str | None = None) -> Any:
    """Open a MySQL connection, with a diagnosis rather than a traceback.

    The three ways this fails in practice - driver missing, nothing configured,
    server down - each need a different fix, so each says so.
    """
    try:
        import pymysql
    except ImportError as exc:                              # pragma: no cover
        raise RuntimeError(
            "PyMySQL is not installed. Run: pip install -e .[sink]") from exc

    if not settings.mysql.configured:
        raise RuntimeError(
            "No MySQL host configured. Set SCRAPEV3_MYSQL_HOST in .env")

    return pymysql.connect(**settings.mysql.connect_kwargs(database))


def open_tns_sink(settings: Settings, *, dry_run: bool = False) -> TnsSink:
    """Connect, load the agency directory, and return a ready sink.

    The directory load is one query up front rather than a lookup per article:
    34k rows is nothing, and this data changes on a human timescale.
    """
    conn = connect(settings, settings.mysql.sink_db)
    directory = AgencyDirectory.load(
        conn, db=settings.mysql.sink_db, group_filter=settings.tns.group_filter)
    return TnsSink(
        conn, directory,
        db=settings.mysql.sink_db,
        status=settings.tns.status,
        goback_chars=settings.tns.filename_chars,
        min_words=settings.tns.min_words,
        short_doc_max_words=settings.tns.short_doc_max_words,
        dry_run=dry_run,
    )
