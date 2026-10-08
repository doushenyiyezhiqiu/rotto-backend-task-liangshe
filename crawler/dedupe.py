"""Cross-board deduplication -> canonical_job_id.

1. Blocking: listings are bucketed by (normalised company, normalised title) and, as a second
   pass for agency-vs-employer cases, by (normalised title, city).
2. Scoring within a bucket: description token containment (wording differs between boards,
   so set overlap of content words beats exact shingles) + bonuses for same city, matching
   salary and close posting dates.
3. Matching: greedy one-to-one per pair of boards (a listing links to at most one listing on
   each other board), so a bucket like "Tesco / Customer Assistant / London" with 30 stores
   does not collapse into one job.
4. Groups = connected components. Each group keeps the canonical id already held by its
   oldest member, so ids are stable across runs; otherwise a fresh deterministic id.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .models import provisional_canonical_id

STOP = set("""a an and are as at be by for from has have in is it its of on or our that the
this to we will with you your their they who what which can all any if not but more
role job work team experience skills about us also within working including""".split())

COMPANY_SUFFIX = re.compile(
    r"\b(ltd|limited|plc|llp|llc|inc|uk|group|holdings|the|co|company|services|international)\b")
TITLE_NOISE = re.compile(
    r"\b(urgent(ly)?|immediate start|new|hiring|required|wanted|needed|apply now|full time|"
    r"part time|permanent|temporary|temp|contract|remote|hybrid|ft|pt)\b")

MAX_BUCKET = 200
LINK_THRESHOLD = 0.62


def norm_company(s: str | None) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = COMPANY_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"£[\d,.k]+.*$", " ", s)               # trailing salary text
    s = re.sub(r"\s[-|–]\s.*$", " ", s)               # "Title - London" / "Title | Company"
    s = s.replace("&", " and ").replace("snr", "senior").replace("jnr", "junior")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = TITLE_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def city(location: str | None) -> str:
    loc = (location or "").lower()
    loc = re.sub(r"\(.*?\)", " ", loc)
    first = re.split(r"[,/]", loc)[0]
    first = re.sub(r"\b(greater|city of|central|north|south|east|west|remote|hybrid)\b", " ", first)
    first = re.sub(r"[^a-z ]+", " ", first)
    return re.sub(r"\s+", " ", first).strip()


def tokens(desc: str | None) -> set:
    words = re.findall(r"[a-z][a-z0-9+#]{2,}", (desc or "").lower())
    return {w for w in words if w not in STOP}


def containment(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _salary_close(a: dict, b: dict) -> bool:
    if a["salary_min"] is None or b["salary_min"] is None:
        return False
    if a["salary_period"] != b["salary_period"]:
        return False
    x, y = float(a["salary_min"]), float(b["salary_min"])
    return abs(x - y) <= 0.05 * max(x, y)


def _dates_close(a: dict, b: dict) -> bool:
    if not a["posted_at"] or not b["posted_at"]:
        return False
    return abs((a["posted_at"] - b["posted_at"]).total_seconds()) <= 10 * 86400


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def run_dedupe(conn, batch: int = 2000) -> dict:
    meta = conn.execute(
        """SELECT id, source, title, company, location, salary_min, salary_period, posted_at,
                  first_seen_at, canonical_job_id FROM jobs"""
    ).fetchall()
    by_id = {r["id"]: r for r in meta}

    buckets: dict[tuple, list] = defaultdict(list)
    for r in meta:
        t = norm_title(r["title"])
        if not t:
            continue
        c = norm_company(r["company"])
        if c:
            buckets[("ct", c, t)].append(r["id"])
        cy = city(r["location"])
        if cy:
            buckets[("tc", t, cy)].append(r["id"])

    candidate_buckets = []
    need_desc = set()
    for key, ids in buckets.items():
        if len(ids) < 2 or len(ids) > MAX_BUCKET:
            continue
        if len({by_id[i]["source"] for i in ids}) < 2 and key[0] == "tc":
            continue
        candidate_buckets.append((key, ids))
        need_desc.update(ids)

    toks: dict[str, set] = {}
    need = list(need_desc)
    for i in range(0, len(need), batch):
        chunk = need[i:i + batch]
        for r in conn.execute("SELECT id, description FROM jobs WHERE id = ANY(%s)", (chunk,)).fetchall():
            toks[r["id"]] = tokens(r["description"])

    # score candidate pairs
    pairs: dict[tuple, float] = {}
    for key, ids in candidate_buckets:
        kind = key[0]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = by_id[ids[i]], by_id[ids[j]]
                sim = containment(toks.get(a["id"], set()), toks.get(b["id"], set()))
                same_city = city(a["location"]) == city(b["location"]) and city(a["location"]) != ""
                if a["source"] == b["source"]:
                    # same-board reposts: only near-identical text in the same place
                    score = sim if (same_city and sim >= 0.9) else 0.0
                elif kind == "ct":
                    score = sim + (0.3 if same_city else 0) + (0.15 if _salary_close(a, b) else 0) \
                        + (0.05 if _dates_close(a, b) else 0)
                else:  # same title + city, different/unknown company name: needs strong text match
                    score = sim + (0.1 if _salary_close(a, b) else 0) - 0.15
                if score >= LINK_THRESHOLD:
                    k = (a["id"], b["id"]) if a["id"] < b["id"] else (b["id"], a["id"])
                    pairs[k] = max(pairs.get(k, 0), score)

    # greedy one-to-one per board pair
    uf = _UF()
    matched: set[tuple] = set()  # (job_id, other_source)
    for (x, y), _ in sorted(pairs.items(), key=lambda kv: -kv[1]):
        sx, sy = by_id[x]["source"], by_id[y]["source"]
        if (x, sy) in matched or (y, sx) in matched:
            continue
        matched.add((x, sy))
        matched.add((y, sx))
        uf.union(x, y)

    groups: dict[str, list] = defaultdict(list)
    for r in meta:
        groups[uf.find(r["id"])].append(r)

    # stable canonical ids
    used: set[str] = set()
    updates = []
    ordered = sorted(groups.values(), key=lambda g: min((m["first_seen_at"], m["id"]) for m in g))
    for g in ordered:
        g.sort(key=lambda m: (m["first_seen_at"], m["id"]))
        cid = next((m["canonical_job_id"] for m in g
                    if m["canonical_job_id"] and m["canonical_job_id"] not in used), None)
        if cid is None:
            cid = provisional_canonical_id(g[0]["id"])
            if cid in used:
                cid = provisional_canonical_id(g[0]["id"] + "#" + str(len(used)))
        used.add(cid)
        for m in g:
            if m["canonical_job_id"] != cid:
                updates.append((cid, m["id"]))

    for cid, jid in updates:
        conn.execute("UPDATE jobs SET canonical_job_id = %s WHERE id = %s", (cid, jid))
    conn.commit()
    multi = sum(len(g) for g in groups.values() if len(g) > 1)
    return {"groups": len(groups), "duplicates_linked": multi, "canonical_updates": len(updates)}
