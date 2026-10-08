"""HTTP layer: browser-impersonating client, proxy sessions, retries, block detection and
request logging. Knows nothing about job parsing or the jobs table."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("fetch")

# TLS/HTTP2 fingerprints of real browsers. The plain `requests` fingerprint is blocked
# outright by Cloudflare (Indeed) and LinkedIn; curl_cffi replays Chrome/Safari handshakes.
IMPERSONATE = ["chrome", "chrome131", "chrome124", "safari18_0"]

CHALLENGE_MARKERS = [
    ("cloudflare_challenge", ("just a moment...", "cf-chl-", "challenge-platform", "cf_chl_opt")),
    ("captcha", ("hcaptcha", "g-recaptcha", "captcha-delivery", "/captcha/", "px-captcha",
                 "please verify you are a human", "security check")),
    ("access_denied", ("access denied", "request unsuccessful. incapsula", "you have been blocked")),
]


@dataclass
class FetchResult:
    url: str
    status: int | None
    text: str = ""
    final_url: str = ""
    blocked: bool = False
    reason: str | None = None
    not_found: bool = False      # confirmed 404/410
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and not self.blocked


class ProxyAuthError(RuntimeError):
    pass


class RequestLog:
    """Every attempt is logged to request_log (Postgres) and logs/requests-<run>.jsonl."""

    def __init__(self, conn_factory, run_id: int | None):
        self.run_id = run_id
        self._lock = threading.Lock()
        self._buf: list[tuple] = []
        self._conn = conn_factory(autocommit=True) if conn_factory else None
        Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
        self._file = open(Path(config.LOG_DIR) / f"requests-{run_id}.jsonl", "a", encoding="utf-8")

    def add(self, *, source, kind, url, attempt, status, blocked, reason, nbytes, elapsed_ms):
        ts = datetime.now(timezone.utc)
        row = (self.run_id, ts, source, kind, url, attempt, status, blocked, reason, nbytes, elapsed_ms)
        with self._lock:
            self._buf.append(row)
            self._file.write(json.dumps({
                "ts": ts.isoformat(), "source": source, "kind": kind, "url": url, "attempt": attempt,
                "status": status, "blocked": blocked, "reason": reason, "bytes": nbytes,
                "elapsed_ms": elapsed_ms}) + "\n")
            if len(self._buf) >= 50:
                self._flush_locked()

    def _flush_locked(self):
        if not self._buf:
            return
        self._file.flush()
        if self._conn is not None:
            with self._conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO request_log (run_id, ts, source, kind, url, attempt, status, blocked,
                                                reason, bytes, elapsed_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", self._buf)
        self._buf.clear()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def close(self):
        self.flush()
        self._file.close()
        if self._conn is not None:
            self._conn.close()


def classify(status: int | None, text: str, final_url: str, source: str) -> tuple[bool, str | None]:
    """Return (blocked, reason). Being blocked is never treated as evidence about a job."""
    low = text[:20000].lower() if text else ""
    fu = (final_url or "").lower()
    if status == 999:
        return True, "linkedin_999"
    if "authwall" in fu or "/login" in fu or "/uas/login" in fu or "/checkpoint/" in fu:
        return True, "authwall_redirect"
    if status == 429:
        return True, "rate_limited"
    for reason, markers in CHALLENGE_MARKERS:
        # job descriptions may mention "security check" (e.g. DBS checks) - only trust markers
        # on short pages or on error statuses
        if any(m in low for m in markers) and (status in (403, 503) or len(text) < 60000):
            if reason == "captcha" and status == 200 and len(text) > 60000:
                continue
            return True, reason
    if status in (403, 401):
        return True, "forbidden"
    if status == 503 and len(text) < 5000:
        return True, "unavailable_503"
    return False, None


class Fetcher:
    """One per source. Thread-safe: each worker thread gets its own curl_cffi session bound to
    a sticky proxy session id; blocks rotate the session (new IP + new fingerprint)."""

    def __init__(self, source: str, reqlog: RequestLog, *, referer: str | None = None,
                 min_interval: float = 0.0, extra_headers: dict | None = None,
                 warmup_url: str | None = None, impersonate: list | None = None):
        self.source = source
        self.impersonate = impersonate or IMPERSONATE
        # Cloudflare (Indeed) rejects cold sessions but lets a session through once it has
        # loaded the homepage and received its cookies, so each new session visits it first.
        self.warmup_url = warmup_url
        self.reqlog = reqlog
        self.referer = referer
        self.min_interval = min_interval
        self.extra_headers = extra_headers or {}
        self._local = threading.local()
        self._recent = deque(maxlen=60)
        self._recent_lock = threading.Lock()
        self._pause_until = 0.0
        self.stopped = False
        self.bytes = 0

    # -- sessions ---------------------------------------------------------------------------
    def _session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._new_session()
        return s

    def _new_session(self):
        from curl_cffi import requests as creq

        old = getattr(self._local, "session", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        sid = config.new_session_id()
        proxy = config.proxy_for_session(sid)
        imp = random.choice(self.impersonate)
        try:
            s = creq.Session(impersonate=imp)
        except Exception:
            s = creq.Session(impersonate="chrome")
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        s.headers.update({"Accept-Language": "en-GB,en;q=0.9"})
        s.headers.update(self.extra_headers)
        self._local.session = s
        self._local.last = 0.0
        self._local.needs_warm = bool(self.warmup_url)
        return s

    def rotate(self):
        self._new_session()

    # -- health / circuit breaker -------------------------------------------------------------
    def _record(self, blocked: bool):
        with self._recent_lock:
            self._recent.append(blocked)
            if len(self._recent) >= 30 and sum(self._recent) / len(self._recent) > 0.8:
                # Mostly blocked: cool down instead of burning proxy traffic.
                self._pause_until = time.time() + 60
                self._recent.clear()
                log.warning("%s: >80%% of recent requests blocked, pausing 60s", self.source)

    def block_rate(self) -> float:
        with self._recent_lock:
            return sum(self._recent) / len(self._recent) if self._recent else 0.0

    # -- main entry -------------------------------------------------------------------------
    def get(self, url: str, kind: str, *, confirm_not_found: bool = False,
            headers: dict | None = None) -> FetchResult:
        last = FetchResult(url=url, status=None, error="not_attempted")
        for attempt in range(1, config.MAX_ATTEMPTS + 1):
            if self.stopped:
                return FetchResult(url=url, status=None, error="stopped")
            wait = self._pause_until - time.time()
            if wait > 0:
                time.sleep(min(wait, 60))
            res = self._once(url, kind, attempt, headers)
            if res.status == 407:
                raise ProxyAuthError("proxy rejected credentials (HTTP 407) - check PROXY_* env vars")
            if res.ok:
                return res
            if res.status in (404, 410) and not res.blocked:
                if not confirm_not_found:
                    res.not_found = True
                    return res
                # Confirm from a different IP before treating it as removal evidence.
                self.rotate()
                again = self._once(url, "confirm", attempt + 1, headers)
                if again.status in (404, 410) and not again.blocked:
                    again.not_found = True
                    return again
                if again.ok:
                    return again
                last = again
            else:
                last = res
            if res.blocked or res.status is None or (res.status and res.status >= 500):
                self.rotate()
            elif res.status and 400 <= res.status < 500 and res.status not in (408, 425, 429):
                return res  # other 4xx won't improve with retries
            backoff = min(config.BACKOFF_CAP, config.BACKOFF_BASE * (2 ** (attempt - 1)))
            time.sleep(backoff * random.uniform(0.5, 1.0))
        return last

    def _warm(self, s) -> None:
        self._local.needs_warm = False
        t0 = time.time()
        status, nbytes, blocked, reason = None, 0, False, None
        try:
            r = s.get(self.warmup_url, timeout=config.REQUEST_TIMEOUT)
            status, nbytes = r.status_code, len(r.content or b"")
            blocked, reason = classify(status, r.text, str(r.url), self.source)
        except Exception as e:
            reason = "network_error: " + type(e).__name__
        self.bytes += nbytes
        self.reqlog.add(source=self.source, kind="warmup", url=self.warmup_url, attempt=1, status=status,
                        blocked=blocked, reason=reason, nbytes=nbytes,
                        elapsed_ms=int((time.time() - t0) * 1000))
        time.sleep(random.uniform(1.0, 2.5))

    def _once(self, url: str, kind: str, attempt: int, headers: dict | None) -> FetchResult:
        s = self._session()
        if getattr(self._local, "needs_warm", False):
            self._warm(s)
        if self.min_interval:
            gap = time.time() - getattr(self._local, "last", 0.0)
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap + random.uniform(0, self.min_interval / 2))
        h = dict(headers or {})
        if self.referer and "Referer" not in h:
            h["Referer"] = self.referer
        t0 = time.time()
        status, text, final_url, err, nbytes = None, "", url, None, 0
        try:
            r = s.get(url, headers=h, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
            status, final_url = r.status_code, str(r.url)
            content = r.content or b""
            nbytes = len(content) + sum(len(k) + len(v) for k, v in r.headers.items())
            text = content.decode(r.encoding or "utf-8", errors="replace")
        except Exception as e:  # network / TLS / proxy errors
            err = type(e).__name__ + ": " + str(e)[:200]
            if "407" in str(e):
                status = 407
        self._local.last = time.time()
        elapsed = int((time.time() - t0) * 1000)
        blocked, reason = (False, None)
        if status is not None and status != 407:
            blocked, reason = classify(status, text, final_url, self.source)
        elif err:
            reason = "network_error"
        if status and status >= 500 and not blocked:
            reason = f"server_{status}"
        self.bytes += nbytes
        self._record(blocked)
        self.reqlog.add(source=self.source, kind=kind, url=url, attempt=attempt, status=status,
                        blocked=blocked, reason=reason or err, nbytes=nbytes, elapsed_ms=elapsed)
        return FetchResult(url=url, status=status, text=text, final_url=final_url,
                           blocked=blocked, reason=reason, error=err)
