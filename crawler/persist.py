"""Persistence and lifecycle reconciliation.

This is the only module that writes to the jobs table. The live crawler and the offline
fixture runner (`make test`) both call these functions, so the fixture tests exercise the
exact code path used in production.

Rules implemented here:
  * new source id            -> insert, first_seen_at = last_seen_at = now      (jobs_new)
  * observed again           -> last_seen_at advances (never moves backwards),
                                first_seen_at never changes
  * content differs          -> update fields + content_hash                    (jobs_changed)
  * observed while removed   -> removed_at cleared, first_seen_at kept          (reappeared)
  * explicit removal evidence-> removed_at = now, row kept                      (jobs_removed)
  * absence                  -> nothing, unless the caller proves a complete authoritative
                                listing of that scope (remove_absent_from_complete_scope)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (HASH_FIELDS, OBSERVED_FIELDS, content_hash, normalize_observation,
                     provisional_canonical_id)


@dataclass
class RunStats:
    new_ids: set = field(default_factory=set)
    changed_ids: set = field(default_factory=set)
    removed_ids: set = field(default_factory=set)
    reappeared_ids: set = field(default_factory=set)
    seen_ids: set = field(default_factory=set)
    invalid: int = 0

    @property
    def jobs_new(self) -> int:
        return len(self.new_ids)

    @property
    def jobs_changed(self) -> int:
        # A listing first inserted in this run is "new", not also "changed".
        return len(self.changed_ids - self.new_ids)

    @property
    def jobs_removed(self) -> int:
        return len(self.removed_ids)

    def merge(self, other: "RunStats") -> None:
        self.new_ids |= other.new_ids
        self.changed_ids |= other.changed_ids
        self.removed_ids |= other.removed_ids
        self.reappeared_ids |= other.reappeared_ids
        self.seen_ids |= other.seen_ids
        self.invalid += other.invalid


_COLS = ", ".join(OBSERVED_FIELDS)


def upsert_observed(conn, raw_job: dict, now: datetime, stats: RunStats, *,
                    card_hash: str | None = None, refreshed_at: datetime | None = None,
                    next_refresh_at: datetime | None = None) -> str:
    """Record that a live listing was observed with full content. Returns new|changed|unchanged."""
    job = normalize_observation(raw_job)
    jid = job["id"]
    h = content_hash(job)
    row = conn.execute(
        f"SELECT {_COLS}, content_hash, removed_at FROM jobs WHERE id = %s FOR UPDATE", (jid,)
    ).fetchone()

    stats.seen_ids.add(jid)
    if row is None:
        conn.execute(
            f"""INSERT INTO jobs (id, source, source_job_id, {_COLS}, canonical_job_id, content_hash,
                                  first_seen_at, last_seen_at, removed_at, card_hash,
                                  last_detail_at, next_refresh_at, updated_at)
                VALUES (%s, %s, %s, {", ".join(["%s"] * len(OBSERVED_FIELDS))}, %s, %s,
                        %s, %s, NULL, %s, %s, %s, %s)""",
            (jid, job["source"], job["source_job_id"], *[job[f] for f in OBSERVED_FIELDS],
             provisional_canonical_id(jid), h, now, now, card_hash, refreshed_at,
             next_refresh_at, now),
        )
        stats.new_ids.add(jid)
        stats.removed_ids.discard(jid)
        return "new"

    # posted_at: keep the stored value when this observation has none (e.g. a detail page
    # that omits the date); relative-date estimates never count as a content change.
    if job["posted_at"] is None:
        job["posted_at"] = row["posted_at"]

    changed = h != row["content_hash"]
    was_removed = row["removed_at"] is not None
    conn.execute(
        f"""UPDATE jobs SET {", ".join(f"{f} = %s" for f in OBSERVED_FIELDS)},
                content_hash = %s,
                last_seen_at = GREATEST(last_seen_at, %s),
                removed_at = NULL, removal_evidence = NULL,
                card_hash = COALESCE(%s, card_hash),
                last_detail_at = COALESCE(%s, last_detail_at),
                next_refresh_at = COALESCE(%s, next_refresh_at),
                updated_at = %s
            WHERE id = %s""",
        (*[job[f] for f in OBSERVED_FIELDS], h, now, card_hash, refreshed_at,
         next_refresh_at, now, jid),
    )
    if was_removed:
        stats.reappeared_ids.add(jid)
        stats.removed_ids.discard(jid)
    if changed:
        stats.changed_ids.add(jid)
        return "changed"
    return "unchanged"


def mark_seen(conn, job_id: str, now: datetime, stats: RunStats) -> bool:
    """The listing was observed live (e.g. on a search page) without re-reading its content.
    Advances last_seen_at and clears removed_at if it had been removed. Returns False if we
    have no stored row for it yet (the caller then fetches the detail page)."""
    row = conn.execute(
        """UPDATE jobs j SET last_seen_at = GREATEST(j.last_seen_at, %s),
                  removed_at = NULL, removal_evidence = NULL, updated_at = %s
           FROM (SELECT id, removed_at FROM jobs WHERE id = %s FOR UPDATE) old
           WHERE j.id = old.id
           RETURNING old.removed_at AS was_removed""",
        (now, now, job_id),
    ).fetchone()
    if row is None:
        return False
    stats.seen_ids.add(job_id)
    if row["was_removed"] is not None:
        stats.reappeared_ids.add(job_id)
        stats.removed_ids.discard(job_id)
    return True


def mark_removed(conn, job_id: str, now: datetime, evidence: str, stats: RunStats) -> bool:
    """Set removed_at on a live row because we have explicit evidence (expired page,
    confirmed 404, absence from a complete authoritative listing). Row is kept."""
    row = conn.execute(
        """UPDATE jobs SET removed_at = %s, removal_evidence = %s, updated_at = %s,
                  next_refresh_at = NULL
           WHERE id = %s AND removed_at IS NULL
           RETURNING id""",
        (now, evidence, now, job_id),
    ).fetchone()
    if row is None:
        return False
    stats.removed_ids.add(job_id)
    return True


def remove_absent_from_complete_scope(conn, source: str, observed_ids: set, now: datetime,
                                      stats: RunStats,
                                      evidence: str = "absent_from_complete_listing") -> int:
    """Only call this when the caller has a demonstrably complete, authoritative listing of
    every live job of `source` (never for search results or partial crawls)."""
    rows = conn.execute(
        "SELECT id FROM jobs WHERE source = %s AND removed_at IS NULL", (source,)
    ).fetchall()
    n = 0
    for r in rows:
        if r["id"] not in observed_ids and mark_removed(conn, r["id"], now, evidence, stats):
            n += 1
    return n


def set_refresh(conn, job_id: str, refreshed_at: datetime | None, next_refresh_at: datetime | None,
                card_hash: str | None = None) -> None:
    conn.execute(
        """UPDATE jobs SET last_detail_at = COALESCE(%s, last_detail_at), next_refresh_at = %s,
                  card_hash = COALESCE(%s, card_hash)
           WHERE id = %s""",
        (refreshed_at, next_refresh_at, card_hash, job_id),
    )


def live_counts_by_source(conn) -> dict:
    rows = conn.execute(
        "SELECT source, count(*) AS n FROM jobs WHERE removed_at IS NULL GROUP BY source ORDER BY source"
    ).fetchall()
    return {r["source"]: int(r["n"]) for r in rows}


def duplicates_linked(conn) -> int:
    """Number of stored listings whose canonical_job_id is shared with at least one other listing."""
    row = conn.execute(
        """SELECT COALESCE(sum(n), 0) AS n FROM (
               SELECT count(*) AS n FROM jobs GROUP BY canonical_job_id HAVING count(*) > 1) g"""
    ).fetchone()
    return int(row["n"])


__all__ = ["RunStats", "upsert_observed", "mark_seen", "mark_removed",
           "remove_absent_from_complete_scope", "set_refresh", "live_counts_by_source",
           "duplicates_linked", "HASH_FIELDS"]
