"""`status = 0` meant four unrelated things, and the audit kept none of them.

Measured on the first full corpus run: 149 of 1,747 targets unreachable, of
which only 41 carried any explanation. The other 108 were not mysterious - the
explanation was discarded at `audit.py`, which recorded `resp.wall` and had no
branch at all for `resp.error`. So `DNSError`, `robots-disallow` and
`circuit-open` all landed as a bare `status = 0` and were scored as the
publisher's site being broken.

Twenty of them were `.mil` hosts that our own resolver could not answer for.
`nslookup www.centcom.mil 1.1.1.1` returns an address instantly, and with DoH
configured `www.darpa.mil` returns 200 and 40KB of press releases. We published
those agencies to the website as failing sites, for weeks, on the strength of a
resolver fault on this machine.

That is the project's own silent-failure shape pointed at the wrong party, so
it gets the same treatment as any other: a named regression test per bug.
"""

from __future__ import annotations

from scrapev3.audit import TargetAudit, judge
from scrapev3.fetch.client import Response, failure_kind


def _resp(**kw) -> Response:
    base = dict(url="https://x.org/news", final_url="https://x.org/news",
                status=0, text="", headers={}, elapsed_s=0.0)
    base.update(kw)
    return Response(**base)


class TestTheFourProducersOfStatusZero:
    """Each is written by a different line of client.py and means something
    different. Before `failure_kind` nothing downstream could tell them apart."""

    def test_a_transport_dns_failure_is_ours(self):
        assert failure_kind(_resp(error="DNSError: Could not resolve host")) == "dns"

    def test_an_open_circuit_is_ours(self):
        r = _resp(error="circuit-open: host backing off after 5x bot wall")
        assert failure_kind(r) == "circuit"

    def test_a_robots_refusal_is_the_rule_working(self):
        assert failure_kind(_resp(error="robots-disallow")) == "robots"

    def test_a_circuit_message_naming_dns_is_still_a_circuit(self):
        """The reason string embeds the cause, so prefix order matters."""
        r = _resp(error="circuit-open: host backing off after 5x DNSError")
        assert failure_kind(r) == "circuit"

    def test_tls_and_connect_are_distinguished(self):
        assert failure_kind(_resp(error="SSLError: handshake failure")) == "tls"

    def test_the_two_classes_the_re_audit_found_in_the_catch_all(self):
        """Both were landing in "error" - 8 and 7 targets respectively - which
        is the bucket that means "we did not look"."""
        assert failure_kind(_resp(
            error="CertificateVerifyError: Failed to perform, curl: (60) SSL "
                  "certificate")) == "tls"
        assert failure_kind(_resp(
            error="HTTPError: Failed to perform, curl: (92) HTTP/2 stream 1 "
                  "reset by server (error 0x2 INTERNAL_ERROR)")) == "http2"
        assert failure_kind(_resp(error="ConnectionError: reset")) == "connect"
        assert failure_kind(_resp(error="ConnectTimeout: timed out")) == "connect"


class TestTheVocabularyIsClosed:
    """`severity` is closed for the same reason: this reaches a website nobody
    has redeployed, and a word invented later must not read as 'fine'."""

    KNOWN = {"ok", "not_modified", "wall", "robots", "circuit", "dns", "tls",
             "connect", "timeout", "http_4xx", "http_5xx", "http2", "error"}

    def test_every_shape_resolves_to_a_known_word(self):
        for r in (_resp(status=200), _resp(status=304, from_cache=True),
                  _resp(status=403, text="x", wall="access denied"),
                  _resp(status=404), _resp(status=500), _resp(status=0),
                  _resp(error="SomethingNobodyAnticipated: x")):
            assert failure_kind(r) in self.KNOWN

    def test_a_wall_outranks_its_status_code(self):
        """A 403 carrying a challenge is a wall, not an ordinary 4xx."""
        assert failure_kind(_resp(status=403, wall="just a moment")) == "wall"

    def test_a_304_is_not_modified_not_a_failure(self):
        assert failure_kind(_resp(status=304, from_cache=True)) == "not_modified"

    def test_an_unrecognised_error_is_never_silently_ok(self):
        assert failure_kind(_resp(error="WeirdError: x")) != "ok"


class TestTheAuditRecordsWhy:
    """The bug itself: `if resp.ok / elif resp.wall` with no `else`."""

    def test_a_resolver_failure_is_blamed_on_us_not_the_publisher(self):
        a = TargetAudit(a_id=1, domain="centcom.mil",
                        newsroom_url="https://www.centcom.mil/MEDIA")
        a.reachable = False
        a.unreachable_kind = "dns"
        judge(a)
        codes = {f.code for f in a.findings}
        assert "dns" in codes, "the resolver fault must be named"
        assert "unreachable" not in codes, \
            "our broken resolver must not be scored as the site being down"
        assert a.verdict != "broken", \
            "20 .mil agencies were reported broken by exactly this"

    def test_a_robots_refusal_is_not_a_finding_at_all(self):
        """The crawler already argues this for `robots_disallowed`."""
        a = TargetAudit(a_id=2, domain="x.org",
                        newsroom_url="https://x.org/news")
        a.reachable = False
        a.unreachable_kind = "robots"
        judge(a)
        assert a.findings == []
        assert a.verdict == "ok"
        assert any("robots" in n for n in a.notes)

    def test_a_real_refusal_is_still_broken(self):
        """The fix must not launder genuine failures into non-events."""
        a = TargetAudit(a_id=3, domain="x.org",
                        newsroom_url="https://x.org/news")
        a.reachable = False
        a.status = 403
        a.unreachable_kind = "http_4xx"
        judge(a)
        assert a.verdict == "broken"
        assert {f.code for f in a.findings} == {"unreachable"}

    def test_the_kind_survives_serialisation_for_rescore(self):
        """`audit --rescore` re-buckets saved evidence without refetching, so
        the reason has to be in the JSONL rather than only in the console."""
        a = TargetAudit(a_id=4, domain="x.org",
                        newsroom_url="https://x.org/news")
        a.unreachable_kind = "dns"
        assert a.as_dict()["unreachable_kind"] == "dns"
