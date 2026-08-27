"""The `tns.press_release` output contract.

The contract is the part of this system that must not drift: editors read these
documents in a fixed shape, and downstream distribution keys on the filename.
So the composition is tested character by character against what v2 produced,
and the two places where v3 deliberately differs are tested as differences,
not left to be discovered later as surprises.

No database is involved. `record.py` is pure, and `TnsSink` is exercised
against a fake connection that enforces the one constraint that actually
matters - `filename` is UNIQUE.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from scrapev3.tns.agencies import Agency, AgencyDirectory
from scrapev3.tns.record import (PressRelease, Rejected, build_press_release,
                                 clean_for_tns, compose_body, dateline_location,
                                 format_lede, normalise_body, tns_filename,
                                 to_ascii)
from scrapev3.tns.sink import TnsSink

LEDE = ("WASHINGTON, DATE -- The U.S. Department of Agriculture issued the "
        "following news release:")


def body_of(words: int, *, word: str = "sentence") -> str:
    return " ".join([word] * words)


# ---------------------------------------------------------------------------
# filename - the CMS display contract
# ---------------------------------------------------------------------------

def test_filename_matches_v2_byte_for_byte():
    # v2: f"$H {prefix}{date}{title[-goback:]}" where date is YYYY-MM-DD with
    # the dashes removed and the century sliced off.
    assert tns_filename("ams", date(2026, 8, 26), "USDA Announces New Rules") == \
        "$H ams260826 New Rules"


def test_filename_keeps_the_space_after_the_marker():
    """`$H` and the prefix are separated by one space. It is not decoration -
    the CMS parses on it."""
    assert tns_filename("aaaa", date(2026, 1, 2), "headline").startswith("$H aaaa")


def test_filename_tail_shorter_than_goback_uses_the_whole_headline():
    assert tns_filename("x", date(2026, 8, 26), "Hi") == "$H x260826Hi"


def test_filename_widens_deterministically():
    """Widening is how a genuine collision is resolved, so it has to be a pure
    function of the headline - not a counter or a timestamp."""
    headline = "Governor Signs the Transportation Funding Bill"
    narrow = tns_filename("SIL", date(2026, 8, 26), headline, 10)
    wide = tns_filename("SIL", date(2026, 8, 26), headline, 20)
    assert narrow != wide
    assert wide.endswith(headline[-20:])
    assert tns_filename("SIL", date(2026, 8, 26), headline, 20) == wide


# ---------------------------------------------------------------------------
# lede and dateline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("month, expected", [
    (1, "Jan. 5"), (2, "Feb. 5"), (3, "March 5"), (4, "April 5"),
    (5, "May 5"), (6, "June 5"), (7, "July 5"), (8, "Aug. 5"),
    (9, "Sept. 5"), (10, "Oct. 5"), (11, "Nov. 5"), (12, "Dec. 5"),
])
def test_lede_date_is_ap_style_without_a_year(month, expected):
    """March, April, May, June and July are never abbreviated; the rest always
    are; and the year never appears. That is AP style and it is what the wire
    expects."""
    assert format_lede("X, DATE -- Y", date(2026, month, 5)) == f"X, {expected} -- Y"


def test_lede_substitution_leaves_the_rest_of_the_template_alone():
    assert format_lede(LEDE, date(2026, 8, 26)) == (
        "WASHINGTON, Aug. 26 -- The U.S. Department of Agriculture issued the "
        "following news release:")


@pytest.mark.parametrize("lede, expected", [
    ("WASHINGTON, DATE -- x", "WASHINGTON"),
    ("BIRMINGHAM, Ala., DATE -- x", "BIRMINGHAM, Ala."),
    ("SANTA FE, New Mexico, DATE -- x", "SANTA FE, New Mexico"),
    ("HONG KONG, DATE -- x", "HONG KONG"),
])
def test_dateline_location_takes_everything_before_the_placeholder(lede, expected):
    """Non-greedy up to `, DATE`, so a two-part dateline keeps both parts."""
    assert dateline_location(lede) == expected


def test_dateline_location_is_none_when_there_is_no_dateline():
    assert dateline_location("The agency issued the following:") is None


# ---------------------------------------------------------------------------
# text cleanup
# ---------------------------------------------------------------------------

def test_to_ascii_makes_web_punctuation_storable():
    """press_release is latin1. A curly quote is not a cosmetic problem there,
    it is an unstorable byte sequence."""
    assert to_ascii("Don’t “quote” me — please") == \
        'Don\'t "quote" me -- please'


def test_clean_strips_separators_the_body_template_owns():
    """The template inserts its own `* * *` and `***`; a release carrying its
    own copy would make the document ambiguous to whatever splits on them."""
    cleaned = clean_for_tns("Intro\n\n* * *\n\nMore text\n\n###\n")
    assert "* * *" not in cleaned
    assert "###" not in cleaned
    assert "More text" in cleaned


@pytest.mark.parametrize("boilerplate", [
    "FOR IMMEDIATE RELEASE:", "For Immediate Release", "IMMEDIATE RELEASE",
    "(link is external)", "Opens in new window",
])
def test_clean_strips_press_release_boilerplate(boilerplate):
    assert boilerplate not in clean_for_tns(f"{boilerplate} The agency said.")


def test_clean_converts_double_dashes_to_single():
    """unidecode renders an em-dash as `--`; wire style is one hyphen. The
    lede's own ` -- ` is added afterwards, so it is unaffected."""
    assert clean_for_tns("the plan - all of it - passed") == "the plan - all of it - passed"
    assert clean_for_tns("the plan -- all of it -- passed") == "the plan -all of it -passed"


def test_clean_does_not_strip_site_specific_chrome():
    """v2's list carried "Our Clemson" and "Latest News" - per-site knowledge
    leaking into global code, which is the failure mode this rewrite exists to
    end. Anything trafilatura misses belongs in per-domain boilerplate mining,
    learned from the corpus rather than hand-listed."""
    assert "Our Clemson" in clean_for_tns("Our Clemson is a program.")
    assert "Latest News" in clean_for_tns("Latest News about the grant.")


# ---------------------------------------------------------------------------
# body composition
# ---------------------------------------------------------------------------

def test_body_has_the_shape_the_cms_expects():
    body = compose_body("LEDE, Aug. 26 -- x:", "The Headline",
                        "First para.\n\nSecond para.", "https://e.org/a")
    assert body.startswith("LEDE, Aug. 26 -- x:")
    assert "\n\n* * *\n\n" in body
    assert "The Headline" in body
    assert body.endswith("Original text here: https://e.org/a")
    # The headline separator: a bare `*` on its own line between headline and body.
    assert "The Headline\n\n*\n\nFirst para." in body


def test_every_single_newline_becomes_a_paragraph_break():
    """v2's second normalisation rule collapses any run of newlines to exactly
    two, so a single line break from the extractor reads as a paragraph in the
    CMS. It looks like a bug and is the established appearance."""
    assert normalise_body("one\ntwo\n\n\n\nthree") == "one\n\ntwo\n\nthree"


def test_paragraph_break_before_punctuation_survives():
    """v2 wrote this rule as `\\s*\\.`, and `\\s` includes `\\n` - so a paragraph
    beginning with punctuation had its break silently eaten. v3 uses
    `[^\\S\\n]*`, which is the "any whitespace but a newline" that was meant."""
    assert normalise_body("End of para\n\n...continued") == "End of para\n\n...continued"
    # Horizontal whitespace before punctuation is still collapsed, as intended.
    assert normalise_body("a word , and a stop .") == "a word, and a stop."


# ---------------------------------------------------------------------------
# building the row
# ---------------------------------------------------------------------------

def build(**kwargs):
    args = dict(
        a_id=1, prefix="ams", lede_template=LEDE, uname="C22-Editor",
        headline="USDA Announces New Rules", body=body_of(400),
        published=date(2026, 8, 26), url="https://usda.gov/a",
    )
    args.update(kwargs)
    return build_press_release(**args)


def test_a_normal_article_becomes_a_complete_row():
    row = build()
    assert isinstance(row, PressRelease)
    assert row.a_id == 1
    assert row.status == "D"
    assert row.headline2 == ""
    assert row.uname == "C22-Editor"
    assert row.location == "WASHINGTON"
    assert row.content_date == date(2026, 8, 26)
    assert row.filename == "$H ams260826 New Rules"


def test_short_documents_are_routed_to_review_rather_than_dropped():
    row = build(body=body_of(200))
    assert isinstance(row, PressRelease)
    assert row.status == "W"
    assert row.headline2 == "short doc"


def test_documents_at_or_below_the_floor_are_not_loaded():
    """v2 named this `do_not_load_max_words`, which reads as its own opposite.
    The boundary is inclusive: exactly 100 words is rejected."""
    assert isinstance(build(body=body_of(100)), Rejected)
    assert build(body=body_of(100)).reason == "too_short"
    assert isinstance(build(body=body_of(101)), PressRelease)


def test_the_headline_is_removed_before_the_word_count():
    """The extractor routinely repeats the headline as the body's first line,
    and the template adds it back. Counting it would let a duplicated headline
    lift a too-short document over the floor."""
    headline = " ".join(f"word{i}" for i in range(50))
    row = build(headline=headline, body=headline + " " + body_of(60))
    assert isinstance(row, Rejected)
    assert row.reason == "too_short"


def test_a_body_that_will_not_fit_the_column_is_named_not_raised():
    """`body_txt` is TEXT. v2 let MySQL raise and logged a driver error; here
    the reason is a bucket the run reports."""
    row = build(body=body_of(20_000, word="averagelengthword"))
    assert isinstance(row, Rejected)
    assert row.reason == "too_long"


def test_an_agency_with_no_lede_cannot_produce_a_row():
    row = build(lede_template="")
    assert isinstance(row, Rejected)
    assert row.reason == "no_lede"


def test_headline_is_truncated_for_the_column_but_not_for_the_filename():
    """varchar(255): v2 sliced the headline to 254 at insert time while
    building the filename from the whole thing. Both behaviours are kept."""
    headline = "A" * 300 + "DISTINCTIVE"
    row = build(headline=headline)
    assert isinstance(row, PressRelease)
    assert len(row.headline) == 254
    assert row.filename.endswith("ISTINCTIVE")     # last 10 chars


def test_row_carries_what_it_needs_to_rebuild_its_own_filename():
    row = build()
    assert isinstance(row, PressRelease)
    wider = row.with_filename_width(20)
    assert wider.filename != row.filename
    assert wider.body_txt == row.body_txt


# ---------------------------------------------------------------------------
# the sink, against a fake connection that enforces UNIQUE(filename)
# ---------------------------------------------------------------------------

class FakeIntegrityError(Exception):
    """Shaped like the driver's: errno first, as PyMySQL raises it."""


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = None
        self._rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        verb = sql.lstrip().split(None, 1)[0].upper()
        if verb == "INSERT":
            headline, _date, _body, a_id, _status, filename = params[:6]
            if self.db.fail_with is not None:
                raise self.db.fail_with
            if filename in self.db.rows:
                raise FakeIntegrityError(1062, f"Duplicate entry '{filename}'")
            self.db.rows[filename] = (a_id, headline)
            self.db.inserts.append(params)
            self.rowcount = 1
        elif verb == "DELETE":
            doomed = [f for f, (a_id, _h) in self.db.rows.items()
                      if not params or a_id in params]
            for f in doomed:
                del self.db.rows[f]
            self.rowcount = len(doomed)
        elif " IN (" in sql:
            self._rows = [(f,) for f in params if f in self.db.rows]
        else:
            self._result = self.db.rows.get(params[0])

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self):
        self.rows: dict[str, tuple] = {}
        self.inserts: list[tuple] = []
        self.fail_with: Exception | None = None

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def make_sink(**kwargs) -> tuple[TnsSink, FakeConn]:
    conn = FakeConn()
    directory = AgencyDirectory([
        Agency(a_id=1, prefix="ams", lede=LEDE, uname="C22-Editor", name="AMS"),
    ])
    return TnsSink(conn, directory, **kwargs), conn


def load(sink, **kwargs):
    args = dict(a_id=1, headline="USDA Announces New Rules", body=body_of(400),
                published=datetime(2026, 8, 26, 14, 30), url="https://usda.gov/a")
    args.update(kwargs)
    return sink.load(**args)


def test_sink_inserts_the_v2_column_list_plus_location():
    sink, conn = make_sink()
    assert load(sink) == "inserted"
    headline, content_date, body, a_id, status, filename, h2, uname, location = \
        conn.inserts[0]
    assert (a_id, status, uname, location) == (1, "D", "C22-Editor", "WASHINGTON")
    assert content_date == date(2026, 8, 26)      # DATE column, not a datetime
    assert filename == "$H ams260826 New Rules"
    assert h2 == ""
    assert body.startswith("WASHINGTON, Aug. 26 --")
    assert headline == "USDA Announces New Rules"


def test_the_same_document_arriving_twice_is_recognised_not_duplicated():
    sink, conn = make_sink()
    assert load(sink) == "inserted"
    assert load(sink) == "duplicate"
    assert len(conn.inserts) == 1
    assert sink.stats.duplicate == 1


def test_two_different_documents_that_collide_are_both_kept():
    """v2's silent data-loss bug. Same agency, same day, headlines ending
    alike: the filename collides, and v2 dropped the second document. Widening
    the tail is what the per-site FILENAME CHARS column did by hand."""
    sink, conn = make_sink()
    assert load(sink, headline="Senate Panel Approves the Funding Bill") == "inserted"
    assert load(sink, headline="House Panel Rejects the Funding Bill",
                url="https://usda.gov/b") == "inserted"

    assert len(conn.inserts) == 2
    assert sink.stats.widened == 1
    filenames = [row[5] for row in conn.inserts]
    assert len(set(filenames)) == 2
    assert filenames[0].endswith("nding Bill")           # 10-char tail
    assert filenames[1].endswith("he Funding Bill")       # widened one rung, to 15


def test_an_unwidenable_collision_is_reported_rather_than_looping():
    """Two documents whose entire headlines are shorter than the narrowest tail
    cannot be separated by widening. That has to terminate and say so."""
    sink, conn = make_sink()
    assert load(sink, headline="Notice") == "inserted"
    # Same agency, same day, same headline, but a different body: this really
    # is a distinct document, and the filename cannot express the difference.
    conn.rows["$H ams260826Notice"] = (1, "Different Headline Entirely")
    assert load(sink, headline="Notice", body=body_of(500),
                url="https://usda.gov/c") == "duplicate"
    assert any("cannot be widened" in e for e in sink.stats.errors)


def test_a_non_duplicate_error_is_retryable_not_a_rejection():
    """The distinction the whole load-state column exists for: a rejection is a
    verdict, an error is a hiccup, and only one of them should be retried."""
    sink, conn = make_sink()
    conn.fail_with = RuntimeError("MySQL server has gone away")
    assert load(sink) == "insert_error"
    assert sink.stats.insert_error == 1
    assert sink.stats.inserted == 0


def test_an_unknown_agency_is_a_named_outcome():
    sink, _conn = make_sink()
    assert load(sink, a_id=99999) == "no_agency"
    assert sink.stats.no_agency == 1


def test_dry_run_composes_without_writing():
    sink, conn = make_sink(dry_run=True)
    assert load(sink) == "inserted"
    assert conn.inserts == []
    assert len(sink.pending) == 1
    assert sink.pending[0].filename == "$H ams260826 New Rules"


def test_documents_with_no_owner_are_counted_but_still_loaded():
    """`url_grp.uname` is '-1' for the UNASSIGNED group. v2 wrote it through,
    and an unassigned document is still a document - but the count has to be
    visible or the box silently fills up."""
    conn = FakeConn()
    directory = AgencyDirectory([
        Agency(a_id=1, prefix="ams", lede=LEDE, uname="-1", name="AMS")])
    sink = TnsSink(conn, directory)
    assert load(sink) == "inserted"
    assert sink.stats.no_uname == 1


# ---------------------------------------------------------------------------
# the agency directory
# ---------------------------------------------------------------------------

def test_coverage_separates_the_ways_an_agency_can_be_unusable():
    directory = AgencyDirectory([
        Agency(a_id=1, prefix="ams", lede=LEDE, uname="C22-Editor", name="AMS"),
        Agency(a_id=2, prefix="", lede="", uname="-1", name="Broken"),
    ])
    coverage = directory.coverage([1, 2, 3])
    assert coverage["targets"] == 3
    assert coverage["known"] == 2
    assert coverage["missing"] == 1          # a_id 3 has no agency row at all
    assert coverage["unusable"] == 1         # a_id 2 has no prefix or lede
    assert coverage["no_uname"] == 1
    assert coverage["missing_ids"] == [3]


def test_lede_survives_a_driver_that_hands_back_the_raw_blob():
    """`agencies.leads` is a blob. The query CONVERTs it, but a driver that
    returns bytes anyway must not produce a lede of "b'WASHINGTON...'"."""
    agency = Agency(a_id=1, prefix="x", lede=LEDE, uname=None, name="X")
    assert agency.location == "WASHINGTON"
    from scrapev3.tns.agencies import _as_text
    assert _as_text(LEDE.encode("latin1")) == LEDE


def test_schema_identifiers_are_checked_before_interpolation():
    """The schema name is interpolated, not bound, because MySQL will not bind
    an identifier. It comes from configuration, but configuration is a string."""
    from scrapev3.tns.agencies import _safe_ident
    assert _safe_ident("tns_staging") == "tns_staging"
    with pytest.raises(ValueError):
        _safe_ident("tns; DROP TABLE press_release")


# ---------------------------------------------------------------------------
# reconciling the local index against the real table
# ---------------------------------------------------------------------------

def test_missing_filenames_reports_only_what_is_actually_gone():
    """After a TRUNCATE or a restore, the local index still believes articles
    were loaded. It is a cache of what press_release holds, and a cache that
    cannot be invalidated is a trap."""
    sink, conn = make_sink()
    load(sink, headline="Kept Release")
    kept = next(iter(conn.rows))
    assert sink.missing_filenames([kept, "$H gone260826Vanished"]) ==         ["$H gone260826Vanished"]


def test_missing_filenames_batches_without_losing_any():
    """Batched against the unique index so the cost tracks how many articles we
    hold locally, not how many rows the newswire has accumulated."""
    sink, conn = make_sink()
    conn.rows = {f"$H present{i}": (1, "x") for i in range(120)}
    asked = list(conn.rows) + [f"$H absent{i}" for i in range(80)]
    missing = sink.missing_filenames(asked, batch=7)
    assert len(missing) == 80
    assert all(f.startswith("$H absent") for f in missing)


def test_load_state_survives_a_round_trip_and_can_be_reset(tmp_path):
    from scrapev3.extract.models import Article, DateResult
    from scrapev3.sink import Sink as ArticleSink

    sink = ArticleSink(tmp_path)
    try:
        article = Article(url="https://e.org/a", headline="A Headline",
                          body=body_of(400),
                          date=DateResult(value=datetime(2026, 8, 26)))
        assert sink.write(article, domain="e.org", a_id=1, agency_prefix="ams")

        # Never attempted: pending, and not yet claimed as loaded.
        assert [u for u, _d, _a in sink.pending_tns()] == ["https://e.org/a"]
        assert sink.loaded_tns() == []

        sink.mark_tns(article.url, "loaded", "$H ams260826 Headline")
        assert sink.pending_tns() == []
        assert sink.loaded_tns() == [("https://e.org/a", "$H ams260826 Headline")]

        # The row is gone from MySQL: forgetting makes it pending again.
        sink.reset_tns(["https://e.org/a"])
        assert [u for u, _d, _a in sink.pending_tns()] == ["https://e.org/a"]
    finally:
        sink.close()


def test_a_failed_insert_stays_pending_but_a_rejection_does_not(tmp_path):
    """The distinction the column exists for. An error is a hiccup and must be
    retried; a rejection is a verdict and retrying it forever is noise."""
    from scrapev3.sink import Sink as ArticleSink
    from scrapev3.extract.models import Article, DateResult

    sink = ArticleSink(tmp_path)
    try:
        for n, state in ((1, "error"), (2, "rejected:too_short")):
            article = Article(url=f"https://e.org/{n}", headline="H",
                              body=body_of(400),
                              date=DateResult(value=datetime(2026, 8, 26)))
            sink.write(article, domain="e.org", a_id=1)
            sink.mark_tns(article.url, state)
        assert [u for u, _d, _a in sink.pending_tns()] == ["https://e.org/1"]
    finally:
        sink.close()


# ---------------------------------------------------------------------------
# resetting a test run
# ---------------------------------------------------------------------------

def test_delete_rows_scopes_to_the_agencies_it_is_given():
    sink, conn = make_sink()
    conn.rows = {"$H a": (1, "x"), "$H b": (2, "y"), "$H c": (1, "z")}
    assert sink.delete_rows([1]) == 2
    assert set(conn.rows) == {"$H b"}


def test_delete_rows_with_an_empty_scope_deletes_nothing():
    """The accident worth designing out: a scope that resolved to nothing must
    not fall through to "every row". Only an explicit None means all."""
    sink, conn = make_sink()
    conn.rows = {"$H a": (1, "x"), "$H b": (2, "y")}
    assert sink.delete_rows([]) == 0
    assert len(conn.rows) == 2


def test_delete_rows_with_none_deletes_everything():
    sink, conn = make_sink()
    conn.rows = {"$H a": (1, "x"), "$H b": (2, "y")}
    assert sink.delete_rows(None) == 2
    assert conn.rows == {}


def test_forget_scopes_and_reports_what_it_removed(tmp_path):
    from scrapev3.extract.models import Article, DateResult
    from scrapev3.sink import Sink as ArticleSink

    sink = ArticleSink(tmp_path)
    try:
        for n, dom in ((1, "a.org"), (2, "a.org"), (3, "b.org")):
            sink.write(Article(url=f"https://{dom}/{n}", headline="H", body=body_of(400),
                               date=DateResult(value=datetime(2026, 8, 26))),
                       domain=dom, a_id=n)
        assert sink.forget(domain="nothing.example") == 0
        assert sink.forget(domain="a.org") == 2
        # And the forgotten URLs are fetchable again, which is the entire point.
        assert not sink.seen_url("https://a.org/1")
        assert sink.seen_url("https://b.org/3")
    finally:
        sink.close()


def test_a_dry_run_never_claims_an_article_was_loaded(tmp_path):
    """--dry-run writes nothing to MySQL, so it must not mark the article
    loaded - a later real backfill would skip it and the row would be lost."""
    from scrapev3.crawl import CrawlStats, load_to_tns
    from scrapev3.extract.models import Article, DateResult
    from scrapev3.sink import Sink as ArticleSink

    tns_sink, conn = make_sink(dry_run=True)
    sink = ArticleSink(tmp_path)
    try:
        article = Article(url="https://usda.gov/a", headline="A Headline",
                          body=body_of(400),
                          date=DateResult(value=datetime(2026, 8, 26)))
        sink.write(article, domain="usda.gov", a_id=1)
        stats = CrawlStats()
        assert load_to_tns(tns_sink, sink, article, a_id=1, stats=stats) == "inserted"
        assert conn.inserts == []
        assert sink.loaded_tns() == []
        assert [u for u, _d, _a in sink.pending_tns()] == ["https://usda.gov/a"]
    finally:
        sink.close()
