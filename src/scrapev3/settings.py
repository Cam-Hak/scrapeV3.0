"""Configuration, loaded from environment with sane defaults.

Deliberately plain: no pydantic, no yaml. v1 and v2 both grew a dual-source
config (CSV row + hardcoded template dict) that nobody could reason about, and
v2 shipped two conflicting requirements.txt files. One source, one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _env(key: str, default: str) -> str:
    return os.environ.get(f"SCRAPEV3_{key}", default)


def _env_f(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_i(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader. Existing environment variables always win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Identity:
    """How we present ourselves. Honest at the application layer.

    The plan's posture: modern/non-anomalous at the transport layer (curl_cffi
    fingerprint) but truthful about who we are at the application layer. Do not
    pair a browser User-Agent with this - vendors run cross-layer consistency
    checks, and a Chrome JA4 with a bot UA is itself an anomaly signal.
    """

    user_agent: str = field(default_factory=lambda: _env(
        "USER_AGENT", "TNSNewsBot/1.0 (+https://targetednews.com/bot)"))
    contact_email: str = field(default_factory=lambda: _env(
        "CONTACT_EMAIL", "crawler@targetednews.com"))
    # curl_cffi impersonation target. Pinned, and re-pinned quarterly:
    # impersonating a Chrome version long out of support is its own signal.
    impersonate: str = field(default_factory=lambda: _env("IMPERSONATE", "chrome124"))

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "From": self.contact_email,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }


@dataclass(frozen=True)
class Politeness:
    """Per-host pacing. Every value here is a FLOOR; robots.txt Crawl-delay wins.

    The risk at this scale is burstiness, not volume: 50k sites daily is ~1.5
    req/s aggregate, but draining one host's queue contiguously emits 20
    requests in 10s to one server. Hence concurrency 1 and mandatory jitter.
    """

    default_delay_s: float = field(default_factory=lambda: _env_f("DEFAULT_DELAY_S", 5.0))
    small_site_delay_s: float = field(default_factory=lambda: _env_f("SMALL_SITE_DELAY_S", 10.0))
    max_concurrency_per_host: int = field(default_factory=lambda: _env_i("MAX_CONCURRENCY_PER_HOST", 1))
    # Secondary constraint, added after the Phase 1 survey measured 28.3% of
    # domains sharing an IP with another domain, largest cluster 28. Those
    # clusters turned out to be managed-hosting edges (WP Engine 141.193.213.x,
    # Pantheon 23.185.0.x) fronting hundreds of unrelated customers - so full
    # per-IP serialisation would be absurdly over-conservative, but an
    # unbounded 28-way concurrent burst at one edge is still worth capping.
    max_concurrency_per_ip: int = field(default_factory=lambda: _env_i("MAX_CONCURRENCY_PER_IP", 4))
    jitter_pct: float = field(default_factory=lambda: _env_f("JITTER_PCT", 0.30))
    global_concurrency: int = field(default_factory=lambda: _env_i("GLOBAL_CONCURRENCY", 32))
    request_timeout_s: float = field(default_factory=lambda: _env_f("REQUEST_TIMEOUT_S", 20))
    dns_timeout_s: float = field(default_factory=lambda: _env_f("DNS_TIMEOUT_S", 3))
    # Latency-adaptive backoff: if a host's current latency exceeds this
    # multiple of its rolling p50, double the delay even on HTTP 200.
    latency_backoff_multiple: float = field(default_factory=lambda: _env_f("LATENCY_BACKOFF_MULTIPLE", 2.0))
    # Consecutive refusals (403 or a bot wall) before this host is left alone.
    # A run of them is an answer, not a transient fault: news.csub.edu served a
    # Cloudflare 403 to fourteen consecutive article fetches in one pass, each
    # one paid for with the full per-host delay. Continuing to knock is both
    # impolite and pointless.
    max_consec_refusals: int = field(default_factory=lambda: _env_i("MAX_CONSEC_REFUSALS", 5))
    refusal_cooldown_s: float = field(default_factory=lambda: _env_f("REFUSAL_COOLDOWN_S", 900.0))
    # Redirect hops before giving up. curl's default is 30, and redirects are
    # followed INSIDE curl - they never reach _wait_turn, so a loop emits them
    # back-to-back with no pacing at all. ersnet.org 301s /news-and-features/
    # /news/ to itself forever: ten article fetches became ~300 unpaced
    # requests to one host. A real article is never thirty hops away.
    max_redirects: int = field(default_factory=lambda: _env_i("MAX_REDIRECTS", 5))


@dataclass(frozen=True)
class Ollama:
    host: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "gpt-oss:20b"))
    # Ollama truncates input SILENTLY when it exceeds num_ctx - no error, no
    # warning. The induction code asserts against this before every call.
    num_ctx: int = field(default_factory=lambda: _env_i("OLLAMA_NUM_CTX", 16384))
    max_attempts: int = field(default_factory=lambda: _env_i("INDUCE_MAX_ATTEMPTS", 5))


@dataclass(frozen=True)
class MySQL:
    """Connection details for both databases.

    Two schemas, deliberately separate: `sink_db` (`tns`) is the existing
    newswire CMS database and its schema is not ours to change; `state_db`
    (`scrapev3`) holds crawler state. Nothing here has a default password -
    both predecessor repos committed live credentials, one of them not even
    gitignored.
    """

    host: str = field(default_factory=lambda: _env("MYSQL_HOST", "").strip())
    port: int = field(default_factory=lambda: _env_i("MYSQL_PORT", 3306))
    user: str = field(default_factory=lambda: _env("MYSQL_USER", ""))
    password: str = field(default_factory=lambda: _env("MYSQL_PASSWORD", ""))
    state_db: str = field(default_factory=lambda: _env("MYSQL_STATE_DB", "scrapev3"))
    sink_db: str = field(default_factory=lambda: _env("MYSQL_SINK_DB", "tns"))

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def connect_kwargs(self, database: str | None = None) -> dict:
        kwargs = {
            "host": self.host, "port": self.port,
            "user": self.user, "password": self.password,
            "charset": "utf8mb4", "autocommit": True,
        }
        if database:
            kwargs["database"] = database
        return kwargs


@dataclass(frozen=True)
class Tns:
    """Thresholds governing what becomes a `press_release` row.

    v2's numbers, under names that say what they do - its
    `do_not_load_max_words` meant "reject at or below this", which reads as its
    own opposite.
    """

    # Below this the document is not loaded at all.
    min_words: int = field(default_factory=lambda: _env_i("TNS_MIN_WORDS", 100))
    # Above min_words but at or below this, load with status W for review.
    short_doc_max_words: int = field(
        default_factory=lambda: _env_i("TNS_SHORT_DOC_MAX_WORDS", 250))
    # The editorial box a normal document lands in.
    status: str = field(default_factory=lambda: _env("TNS_STATUS", "D"))
    # Characters of the headline's tail in the `$H` filename.
    filename_chars: int = field(default_factory=lambda: _env_i("TNS_FILENAME_CHARS", 10))
    # `url_grp.descrip` filter, v2's "lede filter". `%` is every group.
    group_filter: str = field(default_factory=lambda: _env("TNS_GROUP_FILTER", "%"))


@dataclass(frozen=True)
class Settings:
    identity: Identity = field(default_factory=Identity)
    politeness: Politeness = field(default_factory=Politeness)
    ollama: Ollama = field(default_factory=Ollama)
    mysql: MySQL = field(default_factory=MySQL)
    tns: Tns = field(default_factory=Tns)
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    # "jsonl" writes files only; "tns" additionally inserts into press_release.
    sink: str = field(default_factory=lambda: _env("SINK", "jsonl").strip().lower())
    # Whether a crawl consults the shared removal list. Off by default: it needs
    # MySQL, and a crawler that cannot reach it should not be silently deciding
    # that nobody has asked to be removed.
    removal: str = field(default_factory=lambda: _env("REMOVAL", "off").strip().lower())
    # Whether a finished pass publishes per-agency health for the website's
    # grid. Off by default for the same reason as `removal`: it needs MySQL, and
    # a dashboard silently frozen at last week's numbers is worse than one that
    # is visibly not there.
    status: str = field(default_factory=lambda: _env("STATUS", "off").strip().lower())

    @property
    def tns_sink_enabled(self) -> bool:
        return self.sink in {"tns", "mysql", "press_release"}

    @property
    def removal_enabled(self) -> bool:
        return self.removal in {"on", "true", "1", "yes"}

    @property
    def status_enabled(self) -> bool:
        return self.status in {"on", "true", "1", "yes"}

    @classmethod
    def load(cls, dotenv: str | Path = ".env") -> "Settings":
        load_dotenv(dotenv)
        s = cls()
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s
