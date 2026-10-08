"""Source interface + shared parsing helpers (JSON-LD JobPosting is common to all boards)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..textutil import (clean, finalize_salary, first, html_to_text, normalize_work_type,
                        parse_date, unit_to_period)


@dataclass
class Card:
    source_job_id: str
    url: str
    fields: dict = field(default_factory=dict)  # partial job fields visible on the search page

    @property
    def card_hash(self) -> str:
        keys = ("title", "company", "location", "salary_text", "salary_min", "salary_max", "work_type")
        raw = json.dumps({k: self.fields.get(k) for k in keys}, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()[:20]


@dataclass
class SearchPage:
    cards: list
    has_more: bool
    total: int | None = None


@dataclass
class Detail:
    status: str            # live | expired | unparseable
    job: dict | None = None
    reason: str | None = None


@dataclass
class Shard:
    key: str
    params: dict


class Source:
    name: str = ""
    referer: str | None = None
    min_interval: float = 0.0
    extra_headers: dict = {}
    warmup_url: str | None = None   # page to load once per new proxy session (cookies)
    impersonate: list | None = None  # browser fingerprints to use (None = fetch.IMPERSONATE)

    def shards(self) -> list[Shard]:
        raise NotImplementedError

    def search_url(self, shard: Shard, page: int) -> str:
        raise NotImplementedError

    def parse_search(self, html: str, url: str) -> SearchPage:
        raise NotImplementedError

    def detail_url(self, source_job_id: str, stored_url: str | None) -> str:
        raise NotImplementedError

    def public_url(self, source_job_id: str, stored_url: str | None = None) -> str:
        raise NotImplementedError

    def parse_detail(self, html: str, url: str, source_job_id: str, card: dict | None) -> Detail:
        raise NotImplementedError


# ------------------------------------------------------------------------- JSON-LD

def jsonld_jobposting(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except Exception:
                continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, dict):
                t = cur.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    return cur
                if "@graph" in cur:
                    stack.append(cur["@graph"])
    return None


def jobposting_fields(jp: dict) -> dict:
    """Map a schema.org JobPosting to our fields (None where absent)."""
    out: dict = {}
    out["title"] = clean(jp.get("title"))
    org = jp.get("hiringOrganization") or {}
    if isinstance(org, list):
        org = org[0] if org else {}
    if isinstance(org, dict):
        out["company"] = clean(org.get("name"))
        logo = org.get("logo")
        if isinstance(logo, dict):
            logo = logo.get("url")
        out["company_logo_url"] = clean(logo) if isinstance(logo, str) and logo.startswith("http") else None
    elif isinstance(org, str):
        out["company"] = clean(org)
    out["description"] = html_to_text(jp.get("description"))
    out["posted_at"] = parse_date(jp.get("datePosted"))
    out["valid_through"] = parse_date(jp.get("validThrough"))
    et = jp.get("employmentType")
    if isinstance(et, list):
        et = " ".join(str(x) for x in et)
    out["work_type"] = normalize_work_type(et)
    out["location"] = jobposting_location(jp)
    sal = jp.get("baseSalary") or jp.get("estimatedSalary")
    if isinstance(sal, list):
        sal = sal[0] if sal else None
    if isinstance(sal, dict):
        cur = (sal.get("currency") or "GBP").upper()
        val = sal.get("value")
        unit = sal.get("unitText")
        lo = hi = None
        if isinstance(val, dict):
            lo, hi = val.get("minValue"), val.get("maxValue")
            if lo is None and hi is None:
                lo = hi = val.get("value")
            unit = val.get("unitText") or unit
        elif isinstance(val, (int, float, str)):
            lo = hi = val
        try:
            lo = float(lo) if lo not in (None, "") else None
            hi = float(hi) if hi not in (None, "") else None
        except (TypeError, ValueError):
            lo = hi = None
        if cur == "GBP" and (lo or hi):
            out["salary_min"], out["salary_max"], out["salary_period"] = finalize_salary(
                lo, hi, unit_to_period(unit) or "year")
    remote = jp.get("jobLocationType") == "TELECOMMUTE"
    out["remote"] = remote
    return out


def jobposting_location(jp: dict) -> str | None:
    locs = jp.get("jobLocation")
    if isinstance(locs, dict):
        locs = [locs]
    parts_all = []
    for loc in locs or []:
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address") or {}
        if isinstance(addr, str):
            parts_all.append(addr)
            continue
        region = addr.get("addressRegion")
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts = [addr.get("addressLocality"), region]
        txt = ", ".join(p for p in parts if p)
        if not txt and country:
            txt = country
        if txt:
            parts_all.append(txt)
    if not parts_all and jp.get("jobLocationType") == "TELECOMMUTE":
        req = jp.get("applicantLocationRequirements")
        name = req.get("name") if isinstance(req, dict) else None
        return f"Remote ({name})" if name else "Remote"
    return clean(parts_all[0]) if parts_all else None


def text_of(el) -> str | None:
    return clean(el.get_text(" ", strip=True)) if el is not None else None


def merge(card: dict | None, detail: dict) -> dict:
    """Detail page wins; card fills gaps (e.g. absolute posting date, salary on Indeed)."""
    out = dict(card or {})
    for k, v in detail.items():
        if v not in (None, "", []):
            out[k] = v
        else:
            out.setdefault(k, None)
    return out


__all__ = ["Card", "SearchPage", "Detail", "Shard", "Source", "jsonld_jobposting",
           "jobposting_fields", "text_of", "merge", "first", "clean", "html_to_text"]
