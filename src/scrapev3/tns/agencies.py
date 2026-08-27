"""The agency directory: `a_id` -> filename prefix, lede template, uname.

Three columns the scraper cannot derive from a web page, all editor-maintained
in `tns`:

* `agencies.filename`  - the `$H` filename prefix (`aaaa`, `ams`, `aphis`)
* `agencies.leads`     - the dateline lede template, a **latin1 blob**
* `url_grp.uname`      - who owns the resulting document, reached through
                         `agencies.ug_id`

Loaded once per run into a dict, exactly as v2 did. 34k rows is nothing, and
the alternative - a lookup per article - would put a query inside the hot loop
for data that changes on a human timescale.

An article whose `a_id` is missing from this directory cannot be written: there
is no prefix for its filename and no lede for its body. That is not a
theoretical gap. The frontier carries 2,399 distinct `a_id`s and this table
knows 2,232 of them, because the site list has kept growing since the agency
data was last loaded. Those are counted and reported, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .record import dateline_location

# v2's query. `CONVERT(... USING latin1)` is required: `leads` is a blob, and
# without the conversion the driver hands back bytes. The join to `url_grp` is
# what supplies `uname` - an agency with no group has no owner and no document.
_QUERY = """
SELECT a.a_id, a.filename, CONVERT(a.leads USING latin1), g.uname, a.agency_name
FROM {db}.agencies a
JOIN {db}.url_grp g ON g.ug_id = a.ug_id
WHERE g.descrip LIKE %s
"""


@dataclass(frozen=True)
class Agency:
    a_id: int
    prefix: str
    lede: str
    uname: str | None
    name: str

    @property
    def location(self) -> str | None:
        return dateline_location(self.lede)

    @property
    def usable(self) -> bool:
        """Enough to build a row. `uname` may legitimately be missing."""
        return bool(self.prefix) and bool(self.lede)


class AgencyDirectory:
    """In-memory `a_id` -> `Agency`, loaded from `tns` in one query."""

    def __init__(self, agencies: Iterable[Agency] = ()):
        self._by_id: dict[int, Agency] = {a.a_id: a for a in agencies}

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, a_id: object) -> bool:
        return a_id in self._by_id

    def get(self, a_id: int) -> Agency | None:
        return self._by_id.get(a_id)

    @classmethod
    def load(cls, conn: Any, *, db: str = "tns", group_filter: str = "%") -> "AgencyDirectory":
        """Read the directory. `group_filter` matches `url_grp.descrip`.

        v2 called this the "lede filter" and used it to run one editorial group
        at a time; `%` is every group.
        """
        with conn.cursor() as cur:
            cur.execute(_QUERY.format(db=_safe_ident(db)), (group_filter,))
            rows = cur.fetchall()
        return cls(
            Agency(
                a_id=int(r[0]),
                prefix=(r[1] or "").strip(),
                lede=_as_text(r[2]).strip(),
                uname=(r[3] or None),
                name=(r[4] or ""),
            )
            for r in rows
        )

    def coverage(self, a_ids: Iterable[int]) -> dict[str, Any]:
        """How much of a target list this directory can actually serve.

        Worth printing before a run rather than discovering mid-crawl that a
        tenth of the corpus has nowhere to land.
        """
        wanted = sorted(set(a_ids))
        known = [i for i in wanted if i in self._by_id]
        missing = [i for i in wanted if i not in self._by_id]
        no_uname = [i for i in known if not self._by_id[i].uname
                    or self._by_id[i].uname == "-1"]
        unusable = [i for i in known if not self._by_id[i].usable]
        return {
            "targets": len(wanted),
            "known": len(known),
            "missing": len(missing),
            "missing_ids": missing[:20],
            "no_uname": len(no_uname),
            "unusable": len(unusable),
        }


def _as_text(value: Any) -> str:
    """`leads` arrives as str when CONVERTed, bytes if the driver hands the blob
    through anyway. Accept both."""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("latin1", errors="replace")
    return "" if value is None else str(value)


def _safe_ident(name: str) -> str:
    """Schema names are interpolated, not bound, so they get checked.

    Only ever fed from configuration, but configuration is still a string.
    """
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe schema identifier: {name!r}")
    return name
