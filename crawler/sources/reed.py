"""Reed (reed.co.uk). Next.js site: search and job pages embed their data in __NEXT_DATA__.

Search page:  props.pageProps.searchResults.jobs[] -> {jobDetail: {...}, url, logoImage}
Job page:     props.pageProps.consolidatedJobDetails.jobDetails -> full job

Field choices (verified against live pages, Oct 2026):
  * posted_at  = displayDate (what Reed shows as "Posted 23 September"), UK local time.
                 createdDate / JSON-LD datePosted is the original creation date, often years old.
  * location   = locationName + ", " + countyName, exactly as shown ("Enfield Town, Middlesex").
  * salary     = the displayed salary text ("£16.13 - £16.45 per hour"); hidden salaries
                 ("Competitive salary") give null even though placeholder numbers exist.
  * company    = the "Posted by" profile name; logo = that profile's logo.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ..textutil import (clean, finalize_salary, first, html_to_text, normalize_work_type,
                        parse_date, parse_salary_text, walk)
from .base import Card, Detail, SearchPage, Shard, Source, jobposting_fields, jsonld_jobposting, merge, text_of
from .locations import BIG_LOCATIONS, KEYWORDS, UK_LOCATIONS, slug

BASE = "https://www.reed.co.uk"
LOGO_BASE = "https://resources.reed.co.uk/profileimages/logos/thumbs/"
JOB_PATH = re.compile(r"/jobs/[^/?#]+/(\d{6,})")
EXPIRED_MARKERS = ("this job has expired", "job has now expired", "this job is no longer available",
                   "no longer accepting applications", "the job you are looking for has expired")
# Reed salaryType codes seen on live pages: 1 = per hour, 5 = per annum.
SALARY_TYPE = {1: "hour", 5: "year"}
UK_TZ = "Europe/London"


def next_data(html: str):
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _join_location(place: str | None, county: str | None) -> str | None:
    place, county = clean(place), clean(county)
    if place and county and county.lower() not in place.lower():
        return f"{place}, {county}"
    return place or county


def _logo(filename: str | None) -> str | None:
    if not filename:
        return None
    if filename.startswith("http"):
        return filename
    return LOGO_BASE + filename


def _card_salary(d: dict):
    """Search-card salary. salaryDescription != 0 means Reed hides the numbers and shows
    text such as 'Competitive salary' instead."""
    if d.get("salaryDescription"):
        return None, None, None
    lo, hi = d.get("salaryFrom"), d.get("salaryTo")
    lo = float(lo) if isinstance(lo, (int, float)) and lo > 0 else None
    hi = float(hi) if isinstance(hi, (int, float)) and hi > 0 else None
    if lo is None and hi is None:
        return None, None, None
    period = SALARY_TYPE.get(d.get("salaryType"))
    if period is None:  # unknown code: infer from magnitude
        ref = lo or hi
        period = "hour" if ref < 100 else ("day" if ref < 2000 else "year")
    return finalize_salary(lo, hi, period)


def card_from_search_item(item: dict) -> Card | None:
    d = item.get("jobDetail") if isinstance(item.get("jobDetail"), dict) else item
    jid = d.get("jobId")
    if not jid or not d.get("jobTitle"):
        return None
    lo, hi, period = _card_salary(d)
    f = {
        "title": clean(d.get("jobTitle")),
        "company": clean(first(item.get("profileName"), d.get("ouName"))),
        "company_logo_url": _logo(item.get("logoImage")),
        "location": _join_location(d.get("displayLocationName"), d.get("countyLocation")),
        "work_type": normalize_work_type("full time" if d.get("isFullTime") else None,
                                         "part time" if d.get("isPartTime") else None),
        "salary_min": lo, "salary_max": hi, "salary_period": period,
        "posted_at": parse_date(d.get("displayDate"), naive_tz=UK_TZ),
    }
    href = item.get("url")
    link = urljoin(BASE, href) if href else f"{BASE}/jobs/{slug(f['title'] or 'job')}/{jid}"
    return Card(str(jid), link.split("?")[0], f)


def job_from_details(j: dict) -> dict:
    """Map consolidatedJobDetails.jobDetails (job page) to our fields."""
    owner = j.get("jobOwner") or {}
    loc = j.get("jobLocation") or {}
    sal = j.get("jobSalary") or {}
    hours = j.get("jobEmploymentHours") or {}
    contract = (j.get("jobContractType") or {}).get("name")
    lo, hi, period = parse_salary_text(sal.get("displaySalary"))
    location = _join_location(loc.get("locationName"), loc.get("countyName"))
    if not location and loc.get("isRemoteJob"):
        location = "Remote"
    return {
        "title": clean(j.get("title")),
        "company": clean(first(owner.get("profileName"), owner.get("ouName"))),
        "company_logo_url": _logo(first(owner.get("profileLogoImage"), owner.get("ouLogoImage"))),
        "location": location,
        "work_type": normalize_work_type(
            contract if contract and contract.lower() != "permanent" else None,
            "full time" if hours.get("isFullTime") else None,
            "part time" if hours.get("isPartTime") and not hours.get("isFullTime") else None),
        "salary_min": lo, "salary_max": hi, "salary_period": period,
        "posted_at": parse_date(j.get("displayDate"), naive_tz=UK_TZ),
        "description": html_to_text(j.get("description")),
    }


class Reed(Source):
    name = "reed"
    referer = BASE + "/"
    min_interval = 0.5

    def shards(self):
        out = [Shard("all", {"path": "/jobs"})]
        for loc in UK_LOCATIONS:
            out.append(Shard(f"loc:{loc}", {"path": f"/jobs/jobs-in-{slug(loc)}"}))
        for loc in BIG_LOCATIONS:
            for kw in KEYWORDS:
                if kw:
                    out.append(Shard(f"kw:{kw}@{loc}", {"path": f"/jobs/{slug(kw)}-jobs-in-{slug(loc)}"}))
        return out

    def search_url(self, shard: Shard, page: int) -> str:
        q = {"sortby": "DisplayDate"}
        if page > 1:
            q["pageno"] = page
        return f"{BASE}{shard.params['path']}?{urlencode(q)}"

    def parse_search(self, html: str, url: str) -> SearchPage:
        cards: dict[str, Card] = {}
        total = None
        nd = next_data(html)
        if nd:
            sr = (((nd.get("props") or {}).get("pageProps") or {}).get("searchResults") or {})
            items = sr.get("jobs") if isinstance(sr.get("jobs"), list) else None
            if items is not None and isinstance(sr.get("promotedJobs"), list):
                items = items + sr["promotedJobs"]
            for k in ("count", "totalResults"):
                if isinstance(sr.get(k), int):
                    total = sr[k]
                    break
            if items is None:  # layout changed: find job items anywhere
                items = [d for d in walk(nd) if isinstance(d.get("jobDetail"), dict)]
            for it in items:
                c = card_from_search_item(it)
                if c:
                    cards[c.source_job_id] = c
        if not cards:  # HTML fallback
            soup = BeautifulSoup(html, "lxml")
            for art in soup.select("article"):
                a = art.find("a", href=JOB_PATH)
                if not a:
                    continue
                jid = JOB_PATH.search(a["href"]).group(1)
                f = {"title": text_of(art.find(["h2", "h3"])) or text_of(a),
                     "location": text_of(art.select_one("[data-qa='job-metadata-location']"))}
                f["salary_min"], f["salary_max"], f["salary_period"] = parse_salary_text(
                    text_of(art.select_one("[data-qa='job-metadata-salary']")))
                cards[jid] = Card(jid, urljoin(BASE, a["href"]).split("?")[0], f)
        return SearchPage(list(cards.values()), bool(cards), total)

    def detail_url(self, source_job_id: str, stored_url: str | None) -> str:
        return stored_url or f"{BASE}/jobs/job/{source_job_id}"

    def public_url(self, source_job_id: str, stored_url: str | None = None) -> str:
        return (stored_url or f"{BASE}/jobs/job/{source_job_id}").split("?")[0]

    def parse_detail(self, html: str, url: str, source_job_id: str, card: dict | None) -> Detail:
        nd = next_data(html)
        pp = ((nd or {}).get("props") or {}).get("pageProps") or {}
        j = (pp.get("consolidatedJobDetails") or {}).get("jobDetails") or {}
        if j and str(j.get("id")) != str(source_job_id):
            return Detail("unparseable", reason="id_mismatch")
        if j and (j.get("isLive") is False or j.get("isSuspended") is True):
            return Detail("expired", reason="not_live")
        low = html.lower()
        if not j and any(m in low for m in EXPIRED_MARKERS):
            return Detail("expired", reason="expired_marker")

        job = job_from_details(j) if j else {}
        if not job.get("description") or not job.get("title"):
            # Fallback: JSON-LD + rendered HTML (structure change). Note JSON-LD datePosted is
            # the creation date, so it is not used for posted_at.
            soup = BeautifulSoup(html, "lxml")
            jp = jsonld_jobposting(soup)
            ld = jobposting_fields(jp) if jp else {}
            ld.pop("posted_at", None)
            job = merge(ld, job)
            if not job.get("location"):
                job["location"] = text_of(soup.select_one("[data-qa='job-metadata-location']"))
            if not job.get("salary_min"):
                job["salary_min"], job["salary_max"], job["salary_period"] = parse_salary_text(
                    text_of(soup.select_one("[data-qa='job-metadata-salary']")))
        for k in ("valid_through", "remote"):
            job.pop(k, None)
        job = merge(card, job)
        if not job.get("description") or not job.get("title"):
            return Detail("unparseable", reason="no_description_or_title")
        job["url"] = self.public_url(source_job_id, pp.get("canonicalUrl") or url)
        return Detail("live", job)
