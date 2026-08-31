"""Extraction result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class DatePrecision(str, Enum):
    SECOND = "second"
    MINUTE = "minute"
    DAY = "day"
    MONTH = "month"


class Path(str, Enum):
    """Which rung of the cascade produced a field. Recorded per article so we
    can watch the mix drift - a domain that silently falls from `jsonld` to
    `trafilatura` has usually changed CMS."""

    WRAPPER = "wrapper"
    JSONLD = "jsonld"
    MICRODATA = "microdata"
    OPENGRAPH = "opengraph"
    FEED = "feed"
    CMS_API = "cms_api"
    SITEMAP = "sitemap"
    TRAFILATURA = "trafilatura"
    HTMLDATE = "htmldate"
    URL_PATH = "url_path"
    TIME_ELEMENT = "time_element"
    HTTP_HEADER = "http_header"
    LLM = "llm"
    NONE = "none"


@dataclass
class DateResult:
    value: datetime | None = None
    precision: DatePrecision = DatePrecision.DAY
    source: Path = Path.NONE
    raw: str | None = None
    had_offset: bool = False
    # Set when independent sources disagree by more than a week. A rising rate
    # of this per domain is an early layout-drift signal.
    disagreement_days: int | None = None


# The four ways an article fails to be storable, each paired with the sentence
# a person reads. One tuple rather than two lists, so a phrase reworded for
# clarity cannot silently start a new bucket in the fault store.
_UNUSABLE = (
    ("extract_no_headline",     "no headline"),
    ("extract_body_too_short",  "body under 300 chars"),
    ("extract_no_date",         "no date"),
    ("extract_body_is_chrome",  "body looks like page chrome"),
)

#: Just the codes, for `faults` to classify without importing the pairing.
UNUSABLE_CODES = tuple(code for code, _ in _UNUSABLE)


@dataclass
class Article:
    url: str
    headline: str | None = None
    body: str | None = None
    date: DateResult = field(default_factory=DateResult)

    headline_source: Path = Path.NONE
    body_source: Path = Path.NONE

    language: str | None = None
    # Quality signals, carried through to the per-domain baselines that catch
    # silent extraction breakage.
    quality: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def body_len(self) -> int:
        return len(self.body or "")

    @property
    def usable(self) -> bool:
        """Complete enough to store.

        The prose check is what stops a successful-looking extraction of the
        nav sidebar from being written as an article.
        """
        return self.unusable_reason is None

    @property
    def unusable_reason(self) -> str | None:
        """Which requirement failed, or None when the article is storable.

        A bare count of "unusable" is not a diagnosis: seven articles with no
        date and seven whose body came back as the nav menu are the same number
        and completely different problems - the first is a date-extraction gap,
        the second is extraction reading the wrong subtree.
        """
        index = self._unusable_index()
        return None if index is None else _UNUSABLE[index][1]

    @property
    def unusable_code(self) -> str | None:
        """The same verdict as a stable key, for `faults`.

        The phrase is what a person reads and the code is what gets counted
        across runs, so a reworded phrase must not silently start a new bucket.
        Both come from `_UNUSABLE`, one branch chain, so they cannot drift.
        """
        index = self._unusable_index()
        return None if index is None else _UNUSABLE[index][0]

    def _unusable_index(self) -> int | None:
        """Which requirement failed, by position. Ordered cheapest first."""
        if not self.headline:
            return 0
        if self.body_len < 300:
            return 1
        if self.date.value is None:
            return 2
        if self.quality.get("looks_like_navigation", False):
            return 3
        return None
