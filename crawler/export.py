"""output/jobs.jsonl (every stored row, removed included) and output/report.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config
from .models import SCHEMA_FIELDS, to_json_row


def _atomic_write(path: Path, write_fn) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        write_fn(f)
    os.replace(tmp, path)


def export_jobs(conn, out_dir: str | None = None) -> int:
    path = Path(out_dir or config.OUTPUT_DIR) / "jobs.jsonl"
    n = 0

    def write(f):
        nonlocal n
        cur = conn.execute(f"SELECT {', '.join(SCHEMA_FIELDS)} FROM jobs ORDER BY id")
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for r in rows:
                f.write(json.dumps(to_json_row(r), ensure_ascii=False) + "\n")
                n += 1

    _atomic_write(path, write)
    return n


def write_report(report: dict, out_dir: str | None = None) -> None:
    path = Path(out_dir or config.OUTPUT_DIR) / "report.json"
    _atomic_write(path, lambda f: json.dump(report, f, indent=2, default=str))


def request_stats(conn, run_id: int | None) -> dict:
    rows = conn.execute(
        """SELECT COALESCE(status::text, 'network_error') AS status, count(*) AS n,
                  sum(CASE WHEN blocked THEN 1 ELSE 0 END) AS blocked, sum(bytes) AS bytes
           FROM request_log WHERE run_id = %s GROUP BY 1 ORDER BY 1""",
        (run_id,),
    ).fetchall()
    reasons = conn.execute(
        """SELECT COALESCE(source, '?') AS source, reason, count(*) AS n FROM request_log
           WHERE run_id = %s AND blocked GROUP BY 1, 2 ORDER BY 3 DESC""",
        (run_id,),
    ).fetchall()
    by_source = conn.execute(
        """SELECT COALESCE(source, '?') AS source, count(*) AS n,
                  sum(CASE WHEN blocked THEN 1 ELSE 0 END) AS blocked, sum(bytes) AS bytes
           FROM request_log WHERE run_id = %s GROUP BY 1 ORDER BY 1""",
        (run_id,),
    ).fetchall()
    total = sum(int(r["n"]) for r in rows)
    blocked = sum(int(r["blocked"] or 0) for r in rows)
    total_bytes = sum(int(r["bytes"] or 0) for r in rows)
    return {
        "requests_total": total,
        "requests_blocked": blocked,
        "requests_by_status": {r["status"]: int(r["n"]) for r in rows},
        "blocked_by_reason": {f'{r["source"]}:{r["reason"]}': int(r["n"]) for r in reasons},
        "requests_by_source": {r["source"]: {"requests": int(r["n"]), "blocked": int(r["blocked"] or 0),
                                             "bytes": int(r["bytes"] or 0)} for r in by_source},
        "bytes_total": total_bytes,
        "estimated_cost_usd": round(total_bytes / 1e9 * config.PROXY_COST_PER_GB, 4),
    }
