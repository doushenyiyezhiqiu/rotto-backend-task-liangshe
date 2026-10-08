"""`make test`: replay a lifecycle fixture into a fresh, throwaway database using the same
persistence code as the live crawler, and assert the stored rows and report after each run.

Fixture format (tolerant of a few spellings; see tests/fixtures/lifecycle.json):
{
  "runs": [
    {
      "name": "run1",
      "at": "2026-10-01T10:00:00Z",                 # observation time for this run
      "listings": [ {<job fields>, "status": "live"|"expired"|"not_found"|"blocked"|"error"} ],
      "seen": ["reed:1"],                           # observed on a search page, content not re-read
      "removals": [{"id": "reed:2", "evidence": "expired"}],
      "complete_sources": ["reed"],                 # only if that run's listing is complete/authoritative
      "interrupted": false,                         # informational; absence is never removal evidence
      "expect": {
        "jobs": [ {"id": "reed:1", "removed_at": null, "first_seen_at": "2026-10-01T10:00:00Z", ...} ],
        "report": {"jobs_new": 1, "jobs_changed": 0, "jobs_removed": 0}
      }
    }
  ]
}
Expected values: null / literal / "<set>" (non-null) / "<now>" (this run's time) / "<run:NAME>"
(that run's time). content_hash and canonical_job_id are compared structurally (same-or-
different), since hash and id formats are implementation choices.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from . import config, db, persist
from .dedupe import run_dedupe
from .export import export_jobs
from .models import SCHEMA_FIELDS
from .run import build_report
from .textutil import now_utc, parse_date

FAIL_STATUSES = {"blocked", "error", "failed", "timeout", "captcha", "rate_limited"}
REMOVED_STATUSES = {"expired", "removed", "closed", "not_found", "404", "gone", "deleted"}


# --------------------------------------------------------------------------- fixture parsing

def _first_key(d: dict, *keys, default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default


def load_fixture(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    runs = data if isinstance(data, list) else _first_key(data, "runs", "crawls", "steps", default=[])
    out = []
    base = now_utc()
    for i, r in enumerate(runs):
        at = parse_date(_first_key(r, "at", "now", "started_at", "observed_at", "crawled_at", "time"))
        out.append({
            "name": str(_first_key(r, "name", "run", "id", default=f"run{i + 1}")),
            "at": at or base.replace(microsecond=0) + timedelta(hours=i),
            "listings": _first_key(r, "listings", "jobs", "observed", "results", "observations", default=[]) or [],
            "seen": _first_key(r, "seen", "seen_ids", default=[]) or [],
            "removals": _first_key(r, "removals", "removed", "expired", "not_found", default=[]) or [],
            "failures": _first_key(r, "failures", "failed", "blocked", "errors", default=[]) or [],
            "complete_sources": _first_key(r, "complete_sources", "complete_scopes", "complete", default=[]),
            "expect": _first_key(r, "expect", "expected", "expected_state", default={}) or {},
        })
    return out


def _job_id(j) -> str:
    if isinstance(j, str):
        return j
    if j.get("id"):
        return str(j["id"])
    return f'{j["source"]}:{j["source_job_id"]}'


# --------------------------------------------------------------------------- replay

def apply_run(conn, run: dict, stats: persist.RunStats) -> None:
    now = run["at"]
    observed_by_source: dict[str, set] = {}
    for item in run["listings"]:
        jid = _job_id(item)
        status = str(_first_key(item, "status", "result", default="live") or "live").lower()
        http = _first_key(item, "http_status", "status_code")
        if item.get("removed") is True or item.get("expired") is True or status in REMOVED_STATUSES \
                or http in (404, 410):
            ev = str(_first_key(item, "evidence", "reason", default=status if status != "live" else "expired"))
            persist.mark_removed(conn, jid, now, ev, stats)
        elif status in FAIL_STATUSES or (isinstance(http, int) and http >= 400):
            pass  # failed fetch: no information about the listing
        else:
            persist.upsert_observed(conn, item, now, stats)
            observed_by_source.setdefault(jid.split(":", 1)[0], set()).add(jid)
        conn.commit()
    for jid in run["seen"]:
        jid = _job_id(jid)
        if persist.mark_seen(conn, jid, now, stats):
            observed_by_source.setdefault(jid.split(":", 1)[0], set()).add(jid)
    for rm in run["removals"]:
        ev = rm.get("evidence", "expired") if isinstance(rm, dict) else "expired"
        persist.mark_removed(conn, _job_id(rm), now, ev, stats)
    complete = run["complete_sources"]
    if complete is True:
        complete = sorted({_job_id(i).split(":", 1)[0] for i in run["listings"]})
    for scope in complete or []:
        src = scope.get("source") if isinstance(scope, dict) else scope
        persist.remove_absent_from_complete_scope(conn, src, observed_by_source.get(src, set()), now, stats)
    conn.commit()
    run_dedupe(conn)


# --------------------------------------------------------------------------- assertions

class Structural:
    """Checks that an implementation-chosen value (hash, canonical id) is equal exactly
    when the fixture's value is equal."""

    def __init__(self):
        self.fwd, self.back = {}, {}

    def check(self, expected, actual) -> bool:
        if expected is None or actual is None:
            return expected is None and actual is None
        if expected in self.fwd:
            return self.fwd[expected] == actual
        if actual in self.back:
            return self.back[actual] == expected
        self.fwd[expected], self.back[actual] = actual, expected
        return True


def _eq(field, exp, act, run_times, now) -> bool:
    if isinstance(exp, str) and exp.startswith("<") and exp.endswith(">"):
        token = exp[1:-1]
        if token in ("set", "not null", "notnull", "any"):
            return act is not None or token == "any"
        if token == "now":
            exp = now
        elif token.startswith("run:"):
            exp = run_times[token[4:]]
    if exp is None:
        return act is None
    if act is None:
        return False
    if field in ("first_seen_at", "last_seen_at", "removed_at", "posted_at"):
        e = exp if isinstance(exp, datetime) else parse_date(exp)
        return e is not None and abs((e - act).total_seconds()) < 1
    if field in ("salary_min", "salary_max"):
        return abs(float(exp) - float(act)) < 0.005
    if isinstance(act, Decimal):
        act = float(act)
    if isinstance(exp, str) and isinstance(act, str):
        return " ".join(exp.split()) == " ".join(act.split())
    return exp == act


def check_run(conn, run, report, run_times, hash_track, failures) -> None:
    exp = run["expect"]
    rows = {r["id"]: r for r in conn.execute(f"SELECT {', '.join(SCHEMA_FIELDS)} FROM jobs").fetchall()}
    exp_jobs = _first_key(exp, "jobs", "rows", "db", "state", default=None)
    canon = Structural()
    if exp_jobs is not None:
        if isinstance(exp_jobs, dict):
            exp_jobs = [{"id": k, **v} for k, v in exp_jobs.items()]
        exp_ids = {_job_id(j) for j in exp_jobs}
        if exp.get("exact", True):
            extra = set(rows) - exp_ids
            if extra:
                failures.append(f"[{run['name']}] unexpected rows: {sorted(extra)}")
        for j in exp_jobs:
            jid = _job_id(j)
            row = rows.get(jid)
            if row is None:
                failures.append(f"[{run['name']}] missing row {jid}")
                continue
            for field, ev in j.items():
                if field == "id" or (field not in SCHEMA_FIELDS and field != "live"):
                    continue
                if field == "live":
                    if bool(ev) != (row["removed_at"] is None):
                        failures.append(f"[{run['name']}] {jid}.live expected {ev}")
                    continue
                if field == "content_hash":
                    ok = hash_track.setdefault(jid, Structural()).check(ev, row["content_hash"])
                elif field == "canonical_job_id":
                    ok = canon.check(ev, row["canonical_job_id"])
                else:
                    ok = _eq(field, ev, row[field], run_times, run["at"])
                if not ok:
                    failures.append(f"[{run['name']}] {jid}.{field}: expected {ev!r}, got {row[field]!r}")
    for key in ("count", "job_count", "rows_total"):
        if key in exp and int(exp[key]) != len(rows):
            failures.append(f"[{run['name']}] row count expected {exp[key]}, got {len(rows)}")
    exp_report = _first_key(exp, "report", "counts", default={}) or {}
    for k, ev in exp_report.items():
        if k not in report:
            continue
        av = report[k]
        if isinstance(ev, dict) and isinstance(av, dict):
            ok = {kk: int(vv) for kk, vv in ev.items() if vv} == {kk: int(vv) for kk, vv in av.items() if vv}
        else:
            ok = ev == av
        if not ok:
            failures.append(f"[{run['name']}] report.{k}: expected {ev!r}, got {av!r}")


# --------------------------------------------------------------------------- main

def _test_db():
    """Create a throwaway database next to the configured one (never touches real data)."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit, None
    params = conninfo_to_dict(config.DATABASE_URL)
    name = f"rotto_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params["dbname"] = name
    return make_conninfo(**params), name


def _drop_db(name):
    import psycopg
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def main() -> int:
    path = os.getenv("FIXTURE_PATH") or "tests/fixtures/lifecycle.json"
    runs = load_fixture(path)
    dsn, created = _test_db()
    failures: list[str] = []
    try:
        conn = db.connect(dsn)
        db.migrate(conn)
        if not created:
            conn.execute("TRUNCATE jobs, listing_cards, crawl_runs, crawl_progress, request_log")
            conn.commit()
        run_times = {r["name"]: r["at"] for r in runs}
        hash_track: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            for run in runs:
                stats = persist.RunStats()
                apply_run(conn, run, stats)
                report = build_report(conn, None, run["at"], run["at"], stats, {})
                export_jobs(conn, tmp)  # export path is exercised too
                before = len(failures)
                check_run(conn, run, report, run_times, hash_track, failures)
                status = "ok" if len(failures) == before else "FAIL"
                print(f"{run['name']:<20} new={report['jobs_new']} changed={report['jobs_changed']} "
                      f"removed={report['jobs_removed']} live={report['jobs_by_source']}  {status}")
        conn.close()
    finally:
        if created:
            _drop_db(created)
    if failures:
        print(f"\n{len(failures)} assertion(s) failed:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\nall {len(runs)} runs passed ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
