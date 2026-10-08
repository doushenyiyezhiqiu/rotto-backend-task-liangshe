"""The job record shared by parsers, persistence, export and the fixture runner."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .textutil import normalize_ws

SCHEMA_FIELDS = [
    "id", "source", "source_job_id", "url", "title", "company", "company_logo_url",
    "location", "work_type", "salary_min", "salary_max", "salary_period", "posted_at",
    "description", "canonical_job_id", "content_hash", "first_seen_at", "last_seen_at",
    "removed_at",
]

# Fields a source observation can carry. Everything else is lifecycle state we own.
OBSERVED_FIELDS = [
    "url", "title", "company", "company_logo_url", "location", "work_type",
    "salary_min", "salary_max", "salary_period", "posted_at", "description",
]

# What "the listing's content" means for content_hash / jobs_changed.
# posted_at is deliberately excluded: boards show relative ages ("3 days ago"), so our
# estimate drifts between runs even though nothing about the listing changed.
# url is excluded because tracking parameters / slug normalisation are not content.
HASH_FIELDS = [
    "title", "company", "company_logo_url", "location", "work_type",
    "salary_min", "salary_max", "salary_period", "description",
]

SOURCES = ("indeed", "linkedin", "reed")


def make_id(source: str, source_job_id: str) -> str:
    return f"{source}:{source_job_id}"


def _norm_value(field: str, v):
    if v is None:
        return None
    if field in ("salary_min", "salary_max"):
        f = float(v)
        return int(f) if f.is_integer() else round(f, 2)
    if field == "description":
        return normalize_ws(v)
    if isinstance(v, str):
        v = normalize_ws(v)
        return v or None
    return v


def content_hash(job: dict) -> str:
    payload = {f: _norm_value(f, job.get(f)) for f in HASH_FIELDS}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def provisional_canonical_id(job_id: str) -> str:
    return "cj_" + hashlib.sha1(job_id.encode()).hexdigest()[:16]


def normalize_observation(job: dict) -> dict:
    """Return a clean observation dict: id/source/source_job_id + OBSERVED_FIELDS."""
    source = (job.get("source") or "").strip().lower()
    sjid = job.get("source_job_id")
    jid = job.get("id")
    if jid and (not source or sjid is None) and ":" in jid:
        source, sjid = jid.split(":", 1)
    sjid = str(sjid) if sjid is not None else None
    out = {"source": source, "source_job_id": sjid, "id": jid or make_id(source, sjid)}
    for f in OBSERVED_FIELDS:
        v = job.get(f)
        if isinstance(v, str):
            v = v.strip() if f == "description" else normalize_ws(v)
            v = v or None
        out[f] = v
    return out


def missing_required(job: dict) -> list[str]:
    """Fields the grading rules require to identify a listing."""
    req = ["source", "source_job_id", "url", "title", "location", "description"]
    miss = [f for f in req if not job.get(f)]
    if job.get("description") and len(job["description"]) < 40:
        miss.append("description(too short)")
    return miss


def to_json_row(row: dict) -> dict:
    out = {}
    for f in SCHEMA_FIELDS:
        v = row.get(f)
        if isinstance(v, datetime):
            v = v.astimezone(timezone.utc).isoformat()
        elif v is not None and f in ("salary_min", "salary_max"):
            v = float(v)
            v = int(v) if v.is_integer() else v
        out[f] = v
    return out
