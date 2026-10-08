"""All tunables come from environment variables so the graders' run needs no edits."""
from __future__ import annotations

import os
import random
import string
from urllib.parse import quote, unquote, urlsplit, urlunsplit


def _load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from .env when running without make/docker (e.g. on Windows).
    Variables already set in the environment always win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def env_str(name: str, default: str) -> str:
    """docker compose passes unset variables through as empty strings; treat those as unset."""
    v = os.getenv(name)
    return v if v not in (None, "") else default


DATABASE_URL = env_str("DATABASE_URL", "postgresql://rotto:rotto@localhost:5432/rotto")
OUTPUT_DIR = env_str("OUTPUT_DIR", "output")
LOG_DIR = env_str("LOG_DIR", "logs")
DEBUG_DIR = env_str("DEBUG_DIR", "debug")

ALL_SOURCES = ["reed", "indeed", "linkedin"]
SOURCES = [s.strip() for s in env_str("SOURCES", ",".join(ALL_SOURCES)).split(",") if s.strip()] or ALL_SOURCES

# Budgets: the whole run stops cleanly when the wall-clock budget is used up.
CRAWL_MAX_MINUTES = env_float("CRAWL_MAX_MINUTES", 60)
SEARCH_SHARE = env_float("SEARCH_SHARE", 0.35)          # fraction of budget for search pages
MAX_PAGES_PER_SHARD = env_int("MAX_PAGES_PER_SHARD", 100)
MAX_SHARDS = env_int("MAX_SHARDS", 60)                   # per source
MAX_DETAILS_PER_SOURCE = env_int("MAX_DETAILS_PER_SOURCE", 4000)
SEARCH_CONCURRENCY = env_int("SEARCH_CONCURRENCY", 4)    # per source
DETAIL_CONCURRENCY = env_int("DETAIL_CONCURRENCY", 6)    # per source

# Refresh policy (see README): details are re-fetched when the search card changes,
# and otherwise every REFRESH_DAYS (+ jitter) to catch detail-only edits / expiry.
REFRESH_DAYS = env_float("REFRESH_DAYS", 3)
REFRESH_JITTER_DAYS = env_float("REFRESH_JITTER_DAYS", 1)
RESUME_WINDOW_HOURS = env_float("RESUME_WINDOW_HOURS", 24)

# HTTP
MAX_ATTEMPTS = env_int("MAX_ATTEMPTS", 4)
REQUEST_TIMEOUT = env_float("REQUEST_TIMEOUT", 30)
BACKOFF_BASE = env_float("BACKOFF_BASE", 1.5)
BACKOFF_CAP = env_float("BACKOFF_CAP", 30)
STICKY_SESSIONS = env_str("STICKY_SESSIONS", "1") == "1"
PROXY_COST_PER_GB = env_float("PROXY_COST_PER_GB", 8.0)  # residential list price, USD


def _random_id(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def base_proxy_url() -> str | None:
    """PROXY_URL wins; otherwise build from PROXY_HOST / PROXY_USERNAME / PROXY_PASSWORD."""
    url = os.getenv("PROXY_URL")
    if url:
        return url if "://" in url else "http://" + url
    host = os.getenv("PROXY_HOST")
    if not host:
        return None
    port = os.getenv("PROXY_PORT", "")
    if ":" not in host and port:
        host = f"{host}:{port}"
    elif ":" not in host:
        host = f"{host}:7777"
    user = os.getenv("PROXY_USERNAME", "")
    pw = os.getenv("PROXY_PASSWORD", "")
    auth = f"{quote(user, safe='')}:{quote(pw, safe='')}@" if user else ""
    return f"http://{auth}{host}"


def proxy_for_session(session_id: str | None) -> str | None:
    """Oxylabs-style sticky session: append -sessid-<id> to the username so a worker keeps
    one exit IP (and its cookies) until it gets blocked, then rotates."""
    url = base_proxy_url()
    if not url or not session_id or not STICKY_SESSIONS:
        return url
    parts = urlsplit(url)
    if not parts.username or not unquote(parts.username).startswith("customer-"):
        return url  # not an Oxylabs-format username; leave untouched
    user = unquote(parts.username) + f"-sessid-{session_id}-sesstime-10"
    netloc = f"{quote(user, safe='')}:{quote(unquote(parts.password or ''), safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def new_session_id() -> str:
    return _random_id()
