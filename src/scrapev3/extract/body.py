"""Body-text extraction.

trafilatura is the body of record. The evidence for not agonising over this
choice: WCXB (2,008 pages / 1,613 domains, 2026) concludes that on *articles*
every credible extractor converges within F1 0.871-0.932. Article extraction is
saturated. So the engineering effort belongs in **detecting when extraction
silently breaks on a domain**, not in chasing a better extractor.

One rule that is not negotiable, and which v2 violated:

    **Never markdownify raw HTML.**

On the ScrapingHub article benchmark, html2text scores F1 0.662 and
markdownify-class converters score 0.15-0.52, with precision as low as 0.10.
Recall is near-perfect because they keep *everything* - nav, footer, cookie
banner, related links. v2 piped raw soup through html2text, which is a direct
cause of its body-quality problems. The order is always boilerplate removal
first, markdown second.
"""

from __future__ import annotations

import re

# Trailing sections that are not part of the article. Matched only against
# headings *inside* the extracted subtree, never against the raw page.
_TAIL_HEADINGS = re.compile(
    r"^\s*(related|more from|read next|read more|share this|share it|comments?"
    r"|newsletter|subscribe|sign up|follow us|about the author|tags?"
    r"|you may also like|recommended|trending|most read)\b",
    re.IGNORECASE,
)

# Press-release furniture. Safe to strip globally because these are formulaic.
_PR_BOILERPLATE = (
    re.compile(r"^\s*#\s?#\s?#\s*$", re.MULTILINE),
    re.compile(r"^\s*-30-\s*$", re.MULTILINE),
    re.compile(r"FOR IMMEDIATE RELEASE:?\s*", re.IGNORECASE),
    re.compile(r"\(link is external\)", re.IGNORECASE),
    re.compile(r"^\s*Opens in (a )?new (window|tab)\s*$", re.MULTILINE | re.IGNORECASE),
)

# A dateline like "WASHINGTON, D.C. -- " or "CHICAGO - " at the very start.
_DATELINE = re.compile(
    r"^\s*[A-Z][A-Za-z.\s]{2,30}?(?:,\s*[A-Z][A-Za-z.\s]{2,20})?\s*[-–—]{1,3}\s+"
)


def extract_body(html: str, url: str | None = None, *, favor_precision: bool = False) -> str | None:
    """Run trafilatura and return plain text, or None."""
    if not html:
        return None
    try:
        import trafilatura

        return trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=favor_precision,
            deduplicate=True,
        )
    except Exception:
        return None


def extract_body_second_opinion(html: str) -> str | None:
    """Independent extraction used purely as a quality signal.

    Token overlap between two unrelated extractors is a strong unsupervised
    correctness proxy: it gives a quality reading on 100% of domains with zero
    human labelling. Optional dependency - absence just disables the signal.
    """
    if not html:
        return None
    try:
        from resiliparse.extract.html2text import extract_plain_text

        return extract_plain_text(html, main_content=True, alt_texts=False)
    except Exception:
        return None


def token_overlap(a: str | None, b: str | None) -> float:
    """Jaccard overlap on lowercased word sets. 0.0 when either side is empty."""
    if not a or not b:
        return 0.0
    sa = set(re.findall(r"\w+", a.lower()))
    sb = set(re.findall(r"\w+", b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def strip_boilerplate_tail(text: str) -> str:
    """Truncate at the first line that starts a non-article trailing section."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Only consider short standalone lines - a heading, not a sentence that
        # happens to begin with "Related".
        if stripped and len(stripped) <= 60 and _TAIL_HEADINGS.match(stripped):
            # Require some article to already exist, so a page whose first line
            # is "Share this" is not reduced to nothing.
            if sum(len(x) for x in lines[:i]) >= 300:
                return "\n".join(lines[:i]).rstrip()
    return text


def clean_body(text: str | None, *, strip_dateline: bool = True) -> str | None:
    """Normalise whitespace and remove formulaic press-release furniture.

    Deliberately conservative. v2's global cleanup applied
    `re.sub(r"\\s*\\.", ".", body)` to 100% of documents - and because `\\s`
    includes `\\n`, that silently collapsed paragraph breaks before any
    sentence starting with punctuation.
    """
    if not text:
        return None

    out = text.replace("\r\n", "\n").replace("\r", "\n")

    for pattern in _PR_BOILERPLATE:
        out = pattern.sub("", out)

    out = strip_boilerplate_tail(out)

    if strip_dateline:
        out = _DATELINE.sub("", out, count=1)

    # Collapse runs of blank lines to exactly one blank line. Note this only
    # touches newlines, never spacing around punctuation.
    out = re.sub(r"\n{3,}", "\n\n", out)
    # Trailing spaces per line, without touching the line structure.
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)

    try:
        import ftfy

        out = ftfy.fix_text(out)
    except ImportError:
        pass

    out = out.strip()
    return out or None


# A sentence terminator: not preceded by a capital (skips initials and "U.S."),
# followed by whitespace then a capital or end of text.
_SENTENCE_END = re.compile(r"(?<![A-Z])[.!?](?:\s+[A-Z\"“]|\s*$)")


def prose_ratio(text: str | None) -> float:
    """How much of this text reads like prose rather than page furniture.

    Navigation, menus and link lists are many short lines with almost no
    sentence punctuation. Article prose is fewer, longer lines with regular
    full stops. Returns 0.0-1.0.
    """
    if not text:
        return 0.0
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return 0.0

    long_lines = sum(1 for ln in lines if len(ln) >= 60)
    line_score = long_lines / len(lines)

    # Counting ". " naively treats "U.S. Travel Association" as a sentence,
    # which on short text is enough to make a nav menu look like prose. Require
    # the terminator to follow a non-capital (so initials and "U.S." are
    # skipped) and precede a capital.
    sentences = len(_SENTENCE_END.findall(text))
    # Roughly one sentence per 150 chars is normal prose; scale and cap.
    expected = max(1.0, len(text) / 150.0)
    sentence_score = min(1.0, sentences / expected)

    return round((line_score + sentence_score) / 2, 3)


def looks_like_navigation(text: str | None, threshold: float = 0.30) -> bool:
    """True when the extracted 'body' is really page chrome.

    This is the silent failure the whole project exists to catch: the fetch
    succeeds, the extractor returns text, and the text is the nav sidebar.
    Caught in production on ustravel.org/node/352363, whose stored body began
    "View the Main Menu ... Search U.S. Travel Association ... Find Members".

    Threshold calibrated against the real corpus rather than guessed: that nav
    body scores 0.016 and a static "about" page 0.254, while genuine articles
    run 0.44-1.00. 0.30 sits in the gap.
    """
    if not text:
        return True
    return prose_ratio(text) < threshold


def detect_language(text: str | None) -> str | None:
    """Language of the extracted body.

    Run on the *body*, never on raw HTML - nav chrome and boilerplate skew it.
    Replaces v2's approach, which was a 19-word Spanish/French substring
    blocklist masquerading as language detection.
    """
    if not text or len(text) < 40:
        return None
    try:
        from fast_langdetect import detect
    except ImportError:
        return None

    sample = text[:2000].replace("\n", " ").strip()
    # fast_langdetect changed shape between releases: older builds took
    # `low_memory=` and returned a dict, newer ones take `model=` and return a
    # list of candidates. Handle both explicitly rather than swallowing the
    # TypeError - a bare `except Exception` here silently disabled language
    # detection entirely, and every article came back with language=None.
    result = None
    try:
        result = detect(sample, model="lite")
    except TypeError:
        try:
            result = detect(sample, low_memory=True)
        except Exception:
            return None
    except Exception:
        return None

    if isinstance(result, list):
        result = result[0] if result else None
    if isinstance(result, dict):
        lang = result.get("lang")
        return str(lang) if lang else None
    return None
