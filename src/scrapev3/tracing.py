"""Opt-in decision logging.

Discovery makes about a dozen decisions per target and the crawl makes one per
discovered article, and until now the only evidence any of them happened was
the counters at the end. Those say *what* the run did, never *why* it chose
one source over another - so working out why a site yielded nothing meant
re-running it under a hand-written probe script.

Built on the stdlib `logging` module rather than a flag threaded through every
signature: the decision points are spread across four modules and a dozen
functions, and passing a `debug` parameter down to each one would be a larger
change to the code than the logging itself.

Off by default and genuinely free when off - `logging` drops the call before
the arguments are formatted, provided callers use lazy `%s` interpolation
rather than f-strings. That matters in the per-article loop, which runs
hundreds of times per pass.
"""

from __future__ import annotations

import logging
import sys
from urllib.parse import urlsplit

# Everything under this name; each module takes its own child logger, so a
# caller can raise or lower one area on its own.
ROOT = "scrapev3"


class _ConsoleHandler(logging.Handler):
    """Print through a rich Console rather than straight to the stream.

    A progress bar is a *live* display: it redraws its own line in place using
    cursor control. Anything written directly to the terminal lands in the
    middle of that redraw, which is why a bare StreamHandler leaves a trail of
    half-drawn bars with log lines embedded in them.

    Printing through the same Console the bar was given lets rich do what it
    already knows how to do - move the live display down, emit the line above
    it, and redraw. The bar stays on the bottom row and stays accurate.

    `markup` and `highlight` are off because these lines are data: a domain
    containing square brackets should not be read as rich markup, and numbers
    should not be recoloured. `soft_wrap` leaves wrapping to the terminal
    rather than letting rich hard-break a URL mid-token.
    """

    def __init__(self, console) -> None:
        super().__init__()
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.console.print(self.format(record), markup=False,
                               highlight=False, soft_wrap=True)
        except Exception:                                   # noqa: BLE001
            self.handleError(record)


def enable(console=None, level: int = logging.DEBUG) -> None:
    """Turn on decision logging for the whole package.

    Pass the same Console the progress bar uses and the two cooperate; pass
    nothing and lines go to stderr, which is what a script redirecting output
    wants.

    No level column and no timestamp: every line here is one short statement,
    and decorating each with DEBUG and a clock triples the height of the output
    without adding anything. The message *is* the record.

    Idempotent: calling it twice does not double every line.
    """
    logger = logging.getLogger(ROOT)
    if logger.handlers:
        logger.setLevel(level)
        return

    handler = (_ConsoleHandler(console) if console is not None
               else logging.StreamHandler(sys.stderr))
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    # Ours to render, not the root logger's.
    logger.propagate = False


# Domain column. Wide enough for most registrable domains, so the statements
# after it line up and can be read down the page.
TAG = 22


def tag(domain: str) -> str:
    """The left column: which site this line is about.

    Present on every line because a pass runs eight domains at once and their
    decisions interleave.
    """
    return f"{domain[:TAG]:<{TAG}}"


def slug(url: str, width: int = 44) -> str:
    """The identifying tail of a URL.

    Full URLs are mostly a prefix repeated on every line, and they wrap. The
    last path segment is what actually tells one article from another.
    """
    path = urlsplit(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] or path or url
    return tail if len(tail) <= width else tail[:width - 1] + "…"


def get(name: str) -> logging.Logger:
    """A child logger for one module. Silent unless `enable` has been called."""
    return logging.getLogger(name)
