"""Crawl orchestration: search pages -> card bookkeeping -> detail fetches -> persistence.

Fetching (fetch.py), parsing (sources/*) and persistence (persist.py) stay separate; this
module only decides *what* to fetch and hands results to the other layers."""
from __future__ import annotations

import json
import logging
import random
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

from . import config, db, persist
from .dedupe import run_dedupe
from .export import export_jobs, request_stats, write_report
from .fetch import Fetcher, ProxyAuthError, RequestLog
from .models import make_id, missing_required
from .sources import get_source
from .sources.base import Card, Shard, Source
from .textutil import now_utc, parse_date
from .validate import is_uk_listing

log = logging.getLogger("crawl")


class Budget:
    def __init__(self, minutes: float):
        self.start = time.time()
        self.deadline = self.start + minutes * 60
        self.stop = threading.Event()

    def search_deadline(self) -> float:
        return self.start + (self.deadline - self.start) * config.SEARCH_SHARE

    def over(self, at: float | None = None) -> bool:
        return self.stop.is_set() or time.time() >= (at or self.deadline)


class _ThreadConn:
    """One Postgres connection per worker thread."""

    def __init__(self):
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()

    def get(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = db.connect()
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    def close(self):
        for c in self._all:
            try:
                c.close()
            except Exception:
                pass


def _next_refresh(now):
    days = config.REFRESH_DAYS + random.uniform(0, config.REFRESH_JITTER_DAYS)
    return now + timedelta(days=days)


def _save_debug(source: str, name: str, html: str) -> None:
    d = Path(config.DEBUG_DIR) / source
    d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)  # Windows-safe (shard keys contain ':' and '@')
    if len(list(d.glob("*.html"))) < 30:
        (d / f"{name}.html").write_text(html, encoding="utf-8", errors="replace")


# ------------------------------------------------------------------------------- cards

def save_cards(conn, source: str, cards: list[Card], run_id: int, stats: persist.RunStats) -> int:
    """Record search cards; returns how many need a detail fetch. A card seen on a search page
    is evidence the listing is live, so last_seen_at advances (and removed_at clears)."""
    if not cards:
        return 0
    now = now_utc()
    ids = [make_id(source, c.source_job_id) for c in cards]
    jobs = {r["id"]: r for r in conn.execute(
        "SELECT id, card_hash, next_refresh_at, removed_at FROM jobs WHERE id = ANY(%s)", (ids,)).fetchall()}
    known = {r["id"]: r for r in conn.execute(
        "SELECT id, card_hash, invalid_reason, needs_detail FROM listing_cards WHERE id = ANY(%s)",
        (ids,)).fetchall()}
    pending = 0
    for c, jid in zip(cards, ids):
        h = c.card_hash
        j = jobs.get(jid)
        k = known.get(jid)
        if j is not None:
            was_removed = j["removed_at"] is not None
            persist.mark_seen(conn, jid, now, stats)
            due = j["next_refresh_at"] is None or j["next_refresh_at"] <= now
            needs = was_removed or due or j["card_hash"] != h
        elif k is not None and k["invalid_reason"] and k["card_hash"] == h:
            needs = False  # known non-UK / invalid and unchanged
        else:
            needs = True
        needs = needs or bool(k and k["needs_detail"])
        pending += needs
        conn.execute(
            """INSERT INTO listing_cards (id, source, source_job_id, url, card, card_hash,
                                          first_seen_at, last_seen_at, last_run_id, needs_detail)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET url = EXCLUDED.url, card = EXCLUDED.card,
                   card_hash = EXCLUDED.card_hash, last_seen_at = EXCLUDED.last_seen_at,
                   last_run_id = EXCLUDED.last_run_id, needs_detail = EXCLUDED.needs_detail,
                   invalid_reason = CASE WHEN listing_cards.card_hash = EXCLUDED.card_hash
                                         THEN listing_cards.invalid_reason END""",
            (jid, source, c.source_job_id, c.url, json.dumps(c.fields, default=str), h,
             now, now, run_id, needs),
        )
    conn.commit()
    return pending


# ------------------------------------------------------------------------------- per source

class SourceCrawler:
    def __init__(self, source: Source, run_id: int, resume_from: int | None, budget: Budget,
                 reqlog: RequestLog):
        self.src = source
        self.run_id = run_id
        self.resume_from = resume_from
        self.budget = budget
        self.fetcher = Fetcher(source.name, reqlog, referer=source.referer,
                               min_interval=source.min_interval, extra_headers=source.extra_headers,
                               warmup_url=source.warmup_url, impersonate=source.impersonate)
        self.conns = _ThreadConn()
        self.stats = persist.RunStats()
        self.lock = threading.Lock()
        self.summary = {"shards": 0, "shards_complete": 0, "search_pages": 0, "cards": 0,
                        "details_attempted": 0, "details_live": 0, "details_expired": 0,
                        "details_not_found": 0, "details_blocked": 0, "details_unparseable": 0,
                        "invalid_skipped": 0}

    def bump(self, key, n=1):
        with self.lock:
            self.summary[key] += n

    # -- search ------------------------------------------------------------------------------
    def run_search(self):
        conn = self.conns.get()
        shards = self.src.shards()[: config.MAX_SHARDS]
        done = {}
        if self.resume_from:
            for r in conn.execute(
                    "SELECT shard, last_page, done FROM crawl_progress WHERE run_id=%s AND source=%s",
                    (self.resume_from, self.src.name)).fetchall():
                done[r["shard"]] = r
        self.summary["shards"] = len(shards)
        with ThreadPoolExecutor(config.SEARCH_CONCURRENCY) as ex:
            futs = [ex.submit(self._crawl_shard, s, done.get(s.key)) for s in shards]
            for f in as_completed(futs):
                try:
                    f.result()
                except ProxyAuthError:
                    self.budget.stop.set()
                    raise
                except Exception:
                    log.exception("%s: shard failed", self.src.name)

    def _crawl_shard(self, shard: Shard, prev):
        conn = self.conns.get()
        start_page = 1
        if prev is not None:
            if prev["done"]:
                conn.execute(
                    """INSERT INTO crawl_progress (run_id, source, shard, last_page, done)
                       VALUES (%s,%s,%s,%s,true) ON CONFLICT DO NOTHING""",
                    (self.run_id, self.src.name, shard.key, prev["last_page"]))
                conn.commit()
                return
            start_page = prev["last_page"] + 1
        seen_ids: set = set()
        page, complete = start_page, False
        while page <= config.MAX_PAGES_PER_SHARD:
            if self.budget.over(self.budget.search_deadline()):
                break
            url = self.src.search_url(shard, page)
            res = self.fetcher.get(url, "search")
            self.bump("search_pages")
            if not res.ok:
                # blocked / failed pagination: stop this shard, mark incomplete. Never removal evidence.
                log.info("%s %s p%d: %s %s", self.src.name, shard.key, page, res.status, res.reason or res.error)
                break
            sp = self.src.parse_search(res.text, url)
            new = [c for c in sp.cards if c.source_job_id not in seen_ids]
            if not sp.cards and page == 1:
                _save_debug(self.src.name, f"search-empty-{shard.key.replace('/', '_')}", res.text)
            if new:
                seen_ids.update(c.source_job_id for c in new)
                save_cards(conn, self.src.name, new, self.run_id, self.stats)
                self.bump("cards", len(new))
            conn.execute(
                """INSERT INTO crawl_progress (run_id, source, shard, last_page, done)
                   VALUES (%s,%s,%s,%s,false)
                   ON CONFLICT (run_id, source, shard) DO UPDATE SET last_page = EXCLUDED.last_page""",
                (self.run_id, self.src.name, shard.key, page))
            conn.commit()
            if not new or not sp.has_more:
                complete = True
                break
            page += 1
        else:
            complete = True  # reached the configured page cap
        if complete:
            conn.execute(
                """UPDATE crawl_progress SET done = true, complete = true
                   WHERE run_id=%s AND source=%s AND shard=%s""", (self.run_id, self.src.name, shard.key))
            conn.commit()
            self.bump("shards_complete")

    # -- details -----------------------------------------------------------------------------
    def detail_queue(self) -> list[dict]:
        conn = self.conns.get()
        limit = config.MAX_DETAILS_PER_SOURCE
        rows = conn.execute(
            """SELECT c.id, c.source_job_id, c.url AS card_url, c.card, j.url AS job_url,
                      (j.id IS NULL) AS is_new
               FROM listing_cards c LEFT JOIN jobs j ON j.id = c.id
               WHERE c.source = %s AND c.needs_detail AND c.detail_failures < 3
               ORDER BY (j.id IS NULL) DESC, c.last_seen_at DESC
               LIMIT %s""", (self.src.name, limit)).fetchall()
        out = [dict(r) for r in rows]
        if len(out) < limit:
            # Refresh due: catches detail-only edits and expiry of listings that dropped out
            # of search results (absence from search is not evidence, so we look directly).
            due = conn.execute(
                """SELECT j.id, j.source_job_id, NULL AS card_url, c.card, j.url AS job_url, false AS is_new
                   FROM jobs j LEFT JOIN listing_cards c ON c.id = j.id
                   WHERE j.source = %s AND j.removed_at IS NULL AND j.next_refresh_at <= now()
                     AND NOT COALESCE(c.needs_detail, false)
                   ORDER BY j.next_refresh_at LIMIT %s""", (self.src.name, limit - len(out))).fetchall()
            out.extend(dict(r) for r in due)
        return out

    def run_details(self):
        queue = self.detail_queue()
        log.info("%s: %d detail pages queued", self.src.name, len(queue))
        with ThreadPoolExecutor(config.DETAIL_CONCURRENCY) as ex:
            futs = []
            for item in queue:
                if self.budget.over():
                    break
                futs.append(ex.submit(self._detail, item))
            for f in as_completed(futs):
                try:
                    f.result()
                except ProxyAuthError:
                    self.budget.stop.set()
                    ex.shutdown(cancel_futures=True)
                    raise
                except Exception:
                    log.exception("%s: detail failed", self.src.name)

    def _detail(self, item: dict):
        if self.budget.over():
            return
        conn = self.conns.get()
        jid, sjid = item["id"], item["source_job_id"]
        card = item.get("card") or None
        if isinstance(card, str):
            card = json.loads(card)
        if card and card.get("posted_at"):
            card["posted_at"] = parse_date(card["posted_at"])
        url = self.src.detail_url(sjid, item.get("job_url") or item.get("card_url"))
        self.bump("details_attempted")
        res = self.fetcher.get(url, "detail", confirm_not_found=True)
        now = now_utc()

        if res.not_found:
            self.bump("details_not_found")
            persist.mark_removed(conn, jid, now, "not_found_confirmed", self.stats)
            self._card_done(conn, jid)
            conn.commit()
            return
        if not res.ok:
            self.bump("details_blocked")
            return  # leave queued; a blocked page says nothing about the listing

        d = self.src.parse_detail(res.text, res.final_url or url, sjid, card)
        if d.status == "expired":
            self.bump("details_expired")
            persist.mark_removed(conn, jid, now, d.reason or "expired", self.stats)
            self._card_done(conn, jid)
        elif d.status == "unparseable":
            self.bump("details_unparseable")
            _save_debug(self.src.name, f"detail-{sjid}", res.text)
            conn.execute("UPDATE listing_cards SET detail_failures = detail_failures + 1 WHERE id = %s", (jid,))
        else:
            job = {**d.job, "source": self.src.name, "source_job_id": sjid}
            missing = missing_required(job)
            ok_uk, why = is_uk_listing(self.src.name, job.get("location"), job.get("description"))
            if missing or not ok_uk:
                self.bump("invalid_skipped")
                self.stats.invalid += 1
                conn.execute("UPDATE listing_cards SET needs_detail = false, invalid_reason = %s WHERE id = %s",
                             (why or ("missing:" + ",".join(missing)), jid))
                if missing:
                    _save_debug(self.src.name, f"detail-missing-{sjid}", res.text)
            else:
                self.bump("details_live")
                card_hash = conn.execute("SELECT card_hash FROM listing_cards WHERE id=%s", (jid,)).fetchone()
                persist.upsert_observed(conn, job, now, self.stats,
                                        card_hash=card_hash["card_hash"] if card_hash else None,
                                        refreshed_at=now, next_refresh_at=_next_refresh(now))
                self._card_done(conn, jid)
        conn.commit()

    @staticmethod
    def _card_done(conn, jid):
        conn.execute("UPDATE listing_cards SET needs_detail = false, detail_failures = 0 WHERE id = %s", (jid,))

    def run(self):
        try:
            self.run_search()
            if not self.budget.over():
                self.run_details()
        finally:
            self.conns.close()


# ------------------------------------------------------------------------------- run

def _start_run(conn) -> tuple[int, int | None]:
    now = now_utc()
    prev = conn.execute("SELECT id, status, started_at FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    resume_from = None
    if prev and prev["status"] in ("running", "interrupted"):
        if prev["status"] == "running":
            conn.execute("UPDATE crawl_runs SET status='interrupted' WHERE id=%s", (prev["id"],))
        if now - prev["started_at"] < timedelta(hours=config.RESUME_WINDOW_HOURS):
            resume_from = prev["id"]
    row = conn.execute(
        "INSERT INTO crawl_runs (started_at, status, resumed_from) VALUES (%s, 'running', %s) RETURNING id",
        (now, resume_from)).fetchone()
    conn.commit()
    return row["id"], resume_from


def build_report(conn, run_id, started_at, finished_at, stats: persist.RunStats, extra: dict) -> dict:
    rs = request_stats(conn, run_id)
    live = persist.live_counts_by_source(conn)
    canon = conn.execute(
        "SELECT count(DISTINCT canonical_job_id) AS n FROM jobs WHERE removed_at IS NULL").fetchone()["n"]
    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "requests_total": rs["requests_total"],
        "requests_blocked": rs["requests_blocked"],
        "requests_by_status": rs["requests_by_status"],
        "jobs_by_source": live,
        "duplicates_linked": persist.duplicates_linked(conn),
        "jobs_new": stats.jobs_new,
        "jobs_removed": stats.jobs_removed,
        "jobs_changed": stats.jobs_changed,
        "estimated_cost_usd": rs["estimated_cost_usd"],
        # extras (not required by the contract)
        "run_id": run_id,
        "jobs_live_total": sum(live.values()),
        "distinct_canonical_jobs_live": int(canon),
        "jobs_reappeared": len(stats.reappeared_ids),
        "bytes_total": rs["bytes_total"],
        "blocked_by_reason": rs["blocked_by_reason"],
        "requests_by_source": rs["requests_by_source"],
        **extra,
    }


def crawl() -> int:
    conn = db.connect()
    db.migrate(conn)
    run_id, resume_from = _start_run(conn)
    started = now_utc()
    budget = Budget(config.CRAWL_MAX_MINUTES)
    log.info("run %s started (resume_from=%s), budget %.0f min, proxy=%s", run_id, resume_from,
             config.CRAWL_MAX_MINUTES, "yes" if config.base_proxy_url() else "NO PROXY")

    def on_signal(signum, frame):
        log.warning("signal %s: finishing in-flight work and exporting", signum)
        budget.stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    reqlog = RequestLog(db.connect, run_id)
    crawlers = [SourceCrawler(get_source(s), run_id, resume_from, budget, reqlog) for s in config.SOURCES]
    errors = {}

    def go(c: SourceCrawler):
        try:
            c.run()
        except ProxyAuthError as e:
            errors[c.src.name] = str(e)
            log.error("%s", e)
        except Exception as e:
            errors[c.src.name] = repr(e)
            log.exception("%s crashed", c.src.name)

    threads = [threading.Thread(target=go, args=(c,), name=c.src.name) for c in crawlers]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=1)
    reqlog.close()

    stats = persist.RunStats()
    for c in crawlers:
        stats.merge(c.stats)
    dd = run_dedupe(conn)
    finished = now_utc()
    interrupted = budget.stop.is_set()
    extra = {"status": "interrupted" if interrupted else "finished",
             "resumed_from_run": resume_from, "dedupe": dd, "errors": errors,
             "sources": {c.src.name: c.summary for c in crawlers}}
    report = build_report(conn, run_id, started, finished, stats, extra)
    conn.execute("UPDATE crawl_runs SET finished_at=%s, status=%s, report=%s WHERE id=%s",
                 (finished, extra["status"], json.dumps(report, default=str), run_id))
    conn.commit()
    n = export_jobs(conn)
    write_report(report)
    log.info("run %s %s: %d rows exported, new=%d changed=%d removed=%d, requests=%d blocked=%d",
             run_id, extra["status"], n, stats.jobs_new, stats.jobs_changed, stats.jobs_removed,
             report["requests_total"], report["requests_blocked"])
    conn.close()
    return 0
