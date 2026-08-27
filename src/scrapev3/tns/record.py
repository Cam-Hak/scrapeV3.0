"""Composing one `tns.press_release` row.

Pure functions and one dataclass - no database, no network - so the output
contract is testable without a server. The contract itself is not ours to
invent: `tns.press_release` feeds the newswire CMS, and editors read these
documents in a fixed shape. Everything here reproduces v2's
`db/storage.py::db_insert` deliberately, including the odd bits.

The body shape, verbatim from v2::

    {lede}

    * * *

    {headline}
    *
    {description}

    ***

    Original text here: {url}

Three details that look like mistakes and are not:

* **The lede comes from the database, not the page.** `agencies.leads` holds a
  per-agency dateline template - ``WASHINGTON, DATE -- The U.S. Department of
  Agriculture issued the following news release:`` - with a literal ``DATE``
  placeholder. Editors maintain those strings; the scraper substitutes the day
  and otherwise leaves them alone.
* **The date format is AP style with no year** (``Aug. 26``). That is what the
  wire expects.
* **Every single newline becomes a blank line.** v2's normalisation collapses
  runs of newlines to exactly two, so the CMS sees paragraph breaks throughout.

Two things here are deliberate *departures* from v2, both marked below: the
whitespace-before-punctuation rules no longer eat newlines, and the dead
non-breaking-space rule is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date

# AP-style month abbreviations, exactly as v2's `globals.month`. Index 0 is
# unused so a month number indexes directly.
_MONTH = ("", "Jan.", "Feb.", "March", "April", "May", "June", "July",
          "Aug.", "Sept.", "Oct.", "Nov.", "Dec.")

# `text` in MySQL is 65,535 BYTES. Everything we write is ASCII by then, so
# bytes and characters coincide.
BODY_TXT_MAX = 65_535
HEADLINE_MAX = 254          # varchar(255); v2 slices to 254, so we do too
FILENAME_MAX = 100
LOCATION_MAX = 100


def to_ascii(text: str) -> str:
    """Transliterate to ASCII.

    Not cosmetic: `press_release` is a **latin1** table, so a curly quote or an
    em-dash from a web page is not merely ugly, it is unstorable. v2 called
    `unidecode` on the headline at gather time and on the body at insert time;
    doing it in one place here keeps headline, filename and body consistent
    with each other.
    """
    from unidecode import unidecode      # lazy: only the TNS sink needs it

    return unidecode(text or "")


# --------------------------------------------------------------------------
# lede and dateline
# --------------------------------------------------------------------------

def format_lede(lede: str, published: date) -> str:
    """Substitute the day into an agency's lede template."""
    return (lede or "").replace("DATE", f"{_MONTH[published.month]} {published.day}")


# Non-greedy, so "BIRMINGHAM, Ala., DATE --" yields "BIRMINGHAM, Ala." rather
# than stopping at the first comma.
_DATELINE = re.compile(r"^(.{1,100}?),\s*DATE\b")


def dateline_location(lede: str) -> str | None:
    """The place name a lede template opens with, or None if it has no dateline.

    v2 left `press_release.location` NULL. It is derivable from the same string
    the lede comes from, so v3 fills it; this is the one column v3 populates
    that v2 did not.
    """
    m = _DATELINE.match(lede or "")
    return m.group(1).strip()[:LOCATION_MAX] if m else None


# --------------------------------------------------------------------------
# filename - the CMS display contract
# --------------------------------------------------------------------------

def tns_filename(prefix: str, published: date | None, headline: str | None,
                 goback_chars: int = 10) -> str:
    """`$H <prefix><YYMMDD><headline[-goback:]>`, byte-for-byte as v2 built it.

    The space after `$H` is real. `goback_chars` exists because the tail of a
    headline is what disambiguates two same-day documents from one agency - v2
    exposed it as a per-site CSV column and used it on 2 of 2,405 sites.

    It is *not* the dedup key here. That was v2's mistake: two same-day articles
    whose headlines happened to end alike collided silently, and an editor
    fixing a headline caused a re-insert. Dedup lives on canonical-URL and
    content hashes; this string is display only, and the sink widens `goback`
    when a genuine collision shows up.
    """
    date_part = published.strftime("%y%m%d") if published else "000000"
    tail = (headline or "")[-goback_chars:] if goback_chars > 0 else ""
    return f"$H {prefix}{date_part}{tail}"


# --------------------------------------------------------------------------
# body cleanup
# --------------------------------------------------------------------------

# A subset of v2's `replace_defaults`. Two groups are kept for two reasons:
#
#   * The separator patterns (`###`, `* * *`) are load-bearing. The body
#     template below inserts its own `* * *` and `***` markers, and the CMS
#     splits on them - so a release that ships its own copy has to lose it or
#     the document is ambiguous.
#   * `FOR IMMEDIATE RELEASE` and friends are genuine press-release boilerplate
#     that appears across the whole corpus.
#
# What is deliberately NOT ported: v2's list also carried "Our Clemson",
# "Latest News", "Related Stories", "Download PDF" and similar - per-site and
# per-CMS chrome that leaked back into global code, which is the exact failure
# mode this rewrite exists to end. trafilatura removes that class of text
# during extraction; anything it misses belongs in Phase 6's per-domain
# boilerplate mining, learned from the corpus rather than hand-listed.
_STRIP = tuple(re.compile(p) for p in (
    r"#\s?#\s?#",
    r"\*\s?\*\s?\*\s",
    r"##\s\s",
    r"\s~~~~\s",
    r"(__)_*",
    r"FOR IMMEDIATE RELEASE(:)?",
    r"For Immediate Release",
    r"IMMEDIATE RELEASE",
    r"\(link is external\)",
    r"[Oo]pens in new window",
))

_LONG_DASH = re.compile(r"--(\s)?")


def clean_for_tns(text: str) -> str:
    """Strip press-release boilerplate and separators the body template owns.

    Runs after `to_ascii`, which is why the non-breaking-space rule v2 had here
    is gone: `unidecode` has already turned U+00A0 into a plain space, so that
    substitution could never match. It was dead code in v2.
    """
    for pattern in _STRIP:
        text = pattern.sub("", text)
    # unidecode renders an em-dash as "--"; the wire style is a single hyphen.
    # The lede's own " -- " is added after this runs, so it survives.
    return _LONG_DASH.sub("-", text)


# --------------------------------------------------------------------------
# body composition
# --------------------------------------------------------------------------

_BLANK_RUN = re.compile(r"\n\s*\n")
_NEWLINE_RUN = re.compile(r"(\r\n|\r|\n)+")
_AFTER_PERIOD = re.compile(r"\.[^\S\n]+")
_AFTER_QUOTED_PERIOD = re.compile(r'\."[^\S\n]+')
# v2 wrote these two as `\s*`, which includes \n - so a paragraph that happened
# to begin with punctuation had its break silently eaten. `[^\S\n]*` is "any
# whitespace except a newline", which is what was meant.
_BEFORE_PERIOD = re.compile(r"[^\S\n]*\.")
_BEFORE_COMMA = re.compile(r"[^\S\n]*,")
_DOUBLE_SPACE = re.compile(r"  ")


def normalise_body(body: str) -> str:
    """v2's whitespace normalisation, with the newline-eating bug fixed.

    Order matters and is preserved. The second rule is the one with the
    visible effect: it turns every newline run into exactly two, so single
    line breaks from the extractor become paragraph breaks in the CMS.
    """
    body = _BLANK_RUN.sub("\n\n", body)
    body = _NEWLINE_RUN.sub("\n\n", body)
    body = _AFTER_PERIOD.sub(". ", body)
    body = _BEFORE_PERIOD.sub(".", body)
    body = _BEFORE_COMMA.sub(",", body)
    body = _DOUBLE_SPACE.sub(" ", body)
    return _AFTER_QUOTED_PERIOD.sub('." ', body)


def compose_body(lede: str, headline: str, description: str, url: str) -> str:
    """Assemble the document the CMS stores, then normalise it."""
    return normalise_body(
        f"{lede}\n\n* * *\n\n{headline}\n*\n{description}"
        f"\n\n***\n\nOriginal text here: {url}"
    )


# --------------------------------------------------------------------------
# the row
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PressRelease:
    """One row, ready to insert. Column names match `tns.press_release`.

    `prefix` and `headline_full` are not columns. They are what the filename is
    built from, kept so the sink can rebuild it at a wider tail when two
    documents collide, instead of parsing the filename back apart.
    """

    a_id: int
    headline: str
    headline2: str
    content_date: date
    body_txt: str
    status: str
    filename: str
    uname: str | None
    location: str | None
    word_count: int
    prefix: str = ""
    headline_full: str = ""

    def with_filename_width(self, goback_chars: int) -> "PressRelease":
        """The same row under a longer slice of the headline's tail."""
        return replace(self, filename=tns_filename(
            self.prefix, self.content_date, self.headline_full,
            goback_chars)[:FILENAME_MAX])


@dataclass(frozen=True)
class Rejected:
    """Why an article will not become a row. `reason` is a stats bucket key."""

    reason: str
    detail: str = ""


# v2's thresholds, under names that say what they do. `do_not_load_max_words`
# meant "reject at or below this", which read as its own opposite.
MIN_WORDS = 100
SHORT_DOC_MAX_WORDS = 250
DEFAULT_STATUS = "D"


def build_press_release(
    *,
    a_id: int,
    prefix: str,
    lede_template: str,
    uname: str | None,
    headline: str,
    body: str,
    published: date,
    url: str,
    status: str = DEFAULT_STATUS,
    goback_chars: int = 10,
    min_words: int = MIN_WORDS,
    short_doc_max_words: int = SHORT_DOC_MAX_WORDS,
) -> PressRelease | Rejected:
    """Turn an extracted article into a press_release row, or say why not.

    Mirrors v2's ordering exactly, because each step feeds the next: the
    headline is stripped out of the body *before* the word count, so a document
    is not saved from the length gate by a headline the extractor duplicated.
    """
    if not lede_template:
        return Rejected("no_lede", f"a_id {a_id} has no leads template")

    headline = to_ascii(headline).strip()
    if not headline:
        return Rejected("no_headline", url)

    description = clean_for_tns(to_ascii(body))

    # The extractor often repeats the headline as the body's first line, and
    # the template adds it back below.
    description = description.replace(headline, "")

    word_count = len(description.split())
    if word_count <= min_words:
        return Rejected("too_short", f"{word_count} words")

    headline2 = ""
    if word_count <= short_doc_max_words:
        # Box 7: short enough that an editor should look before it goes out.
        status = "W"
        headline2 = "short doc"

    body_txt = compose_body(format_lede(lede_template, published),
                            headline, description, url)
    if len(body_txt) > BODY_TXT_MAX:
        # `body_txt` is TEXT. v2 let MySQL raise and logged the article as
        # rejected; catching it here names the reason instead of a driver error.
        return Rejected("too_long", f"{len(body_txt)} chars")

    return PressRelease(
        a_id=a_id,
        headline=headline[:HEADLINE_MAX],
        headline2=headline2[:HEADLINE_MAX],
        content_date=published,
        body_txt=body_txt,
        status=status,
        filename=tns_filename(prefix, published, headline, goback_chars)[:FILENAME_MAX],
        uname=uname,
        location=dateline_location(lede_template),
        word_count=word_count,
        prefix=prefix,
        headline_full=headline,
    )
