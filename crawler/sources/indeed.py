"""Indeed UK (uk.indeed.com). Search pages embed job cards as JSON in
window.mosaic.providerData["mosaic-provider-jobcards"]; job pages embed window._initialData.
Indeed sits behind Cloudflare, so this is the board most sensitive to fingerprint + IP."""
from __future__ import annotations

import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..textutil import (clean, extract_json_after, finalize_salary, first, html_to_text,
                        normalize_work_type, parse_date, parse_relative_date, parse_salary_text,
                        unit_to_period, walk)
from .base import Card, Detail, SearchPage, Shard, Source, jobposting_fields, jsonld_jobposting, merge, text_of
from .locations import BIG_LOCATIONS, KEYWORDS, UK_LOCATIONS

BASE = "https://uk.indeed.com"
PAGE_SIZE = 10
EXPIRED_MARKERS = ("this job has expired on indeed", "this job has expired", "job is no longer available")
CARD_MARKERS = ('window.mosaic.providerData["mosaic-provider-jobcards"]=',
                'window.mosaic.providerData["mosaic-provider-jobcards"] =')
INITIAL_MARKERS = ("window._initialData=", "window._initialData =", "_initialData=")


def _period(s) -> str | None:
    return unit_to_period(str(s)) if s else None


def _card_from_result(r: dict) -> Card | None:
    jk = r.get("jobkey") or r.get("jobKey")
    if not jk:
        return None
    # The displayed text ("From £27,293 a year") is authoritative; extractedSalary uses -1 for
    # a missing bound, so it is only a fallback.
    lo = hi = period = None
    salary_text = None
    snip = r.get("salarySnippet")
    if isinstance(snip, dict):
        salary_text = snip.get("text")
    if salary_text:
        lo, hi, period = parse_salary_text(salary_text)
    sal = r.get("extractedSalary") or {}
    if lo is None and hi is None and isinstance(sal, dict) and (sal.get("min") or sal.get("max")):
        lo, hi, period = finalize_salary(sal.get("min"), sal.get("max"), _period(sal.get("type")) or "year",
                                         fill=False)
    jt = r.get("jobTypes")
    if isinstance(jt, list):
        jt = " ".join(str(x) for x in jt)
    branding = r.get("companyBrandingAttributes") or {}
    logo = branding.get("logoUrl") if isinstance(branding, dict) else None
    loc = first(r.get("formattedLocation"), r.get("jobLocationCity"))
    if r.get("remoteLocation") and loc and "remote" not in loc.lower():
        loc = f"Remote in {loc}"
    f = {
        "title": clean(first(r.get("displayTitle"), r.get("title"))),
        "company": clean(r.get("company")),
        "company_logo_url": logo if isinstance(logo, str) and logo.startswith("http") else None,
        "location": clean(loc),
        "work_type": normalize_work_type(jt),
        "salary_text": salary_text, "salary_min": lo, "salary_max": hi, "salary_period": period,
        "posted_at": parse_date(r.get("pubDate")) or parse_relative_date(r.get("formattedRelativeTime")),
    }
    return Card(str(jk), f"{BASE}/viewjob?jk={jk}", f)


class Indeed(Source):
    name = "indeed"
    referer = BASE + "/"
    warmup_url = BASE + "/"
    # Only these fingerprints passed Cloudflare after warm-up in live tests (scripts/indeed_diag).
    impersonate = ["chrome", "safari18_0"]
    min_interval = 1.0

    def shards(self):
        out = [Shard("all", {"q": "", "l": "United Kingdom"})]
        for loc in UK_LOCATIONS:
            out.append(Shard(f"loc:{loc}", {"q": "", "l": loc}))
        for loc in BIG_LOCATIONS:
            for kw in KEYWORDS:
                if kw:
                    out.append(Shard(f"kw:{kw}@{loc}", {"q": kw, "l": loc}))
        return out

    def search_url(self, shard: Shard, page: int) -> str:
        q = {"q": shard.params["q"], "l": shard.params["l"], "sort": "date", "radius": "10"}
        if page > 1:
            q["start"] = (page - 1) * PAGE_SIZE
        return f"{BASE}/jobs?{urlencode(q)}"

    def parse_search(self, html: str, url: str) -> SearchPage:
        data = None
        for m in CARD_MARKERS:
            data = extract_json_after(html, m)
            if data:
                break
        cards: dict[str, Card] = {}
        if data:
            results = None
            try:
                results = data["metaData"]["mosaicProviderJobCardsModel"]["results"]
            except (KeyError, TypeError):
                results = [d for d in walk(data) if d.get("jobkey")]
            for r in results or []:
                c = _card_from_result(r)
                if c:
                    cards[c.source_job_id] = c
        if not cards:
            soup = BeautifulSoup(html, "lxml")
            for a in soup.select("a[data-jk]"):
                jk = a["data-jk"]
                box = a.find_parent("td") or a.find_parent("div")
                f = {"title": text_of(a)}
                if box is not None:
                    f["company"] = text_of(box.select_one("[data-testid='company-name']"))
                    f["location"] = text_of(box.select_one("[data-testid='text-location']"))
                cards[jk] = Card(jk, f"{BASE}/viewjob?jk={jk}", f)
        # Indeed shows a "next page" link only while more results exist.
        has_more = bool(cards) and ('data-testid="pagination-page-next"' in html or "pagination-page-next" in html)
        return SearchPage(list(cards.values()), has_more)

    def detail_url(self, source_job_id: str, stored_url: str | None) -> str:
        # /viewjob is behind a stricter Cloudflare rule; a search page with vjk=<jobkey> passes
        # and embeds the same full job view. parse_detail checks the embedded jobKey.
        return f"{BASE}/jobs?q=&l=United+Kingdom&vjk={source_job_id}"

    def public_url(self, source_job_id: str, stored_url: str | None = None) -> str:
        return f"{BASE}/viewjob?jk={source_job_id}"

    def parse_detail(self, html: str, url: str, source_job_id: str, card: dict | None) -> Detail:
        """Works for both a /viewjob page and a search page opened with &vjk=<jobkey>, which
        embeds the same job view under _initialData.autoOpenTwoPaneViewjobResponse.body."""
        data = None
        for m in INITIAL_MARKERS:
            data = extract_json_after(html, m)
            if data:
                break
        body = None
        if isinstance(data, dict):
            auto = data.get("autoOpenTwoPaneViewjobResponse")
            body = auto.get("body") if isinstance(auto, dict) else None
            if body is None and data.get("jobInfoWrapperModel"):
                body = data  # a real /viewjob page
        if not body:
            low = html.lower()
            if any(m in low for m in EXPIRED_MARKERS):
                return Detail("expired", reason="expired_marker")
            return Detail("unparseable", reason="no_job_view")
        if str(body.get("jobKey") or "") != str(source_job_id):
            # vjk was ignored (e.g. job gone) and another job was embedded. Not removal evidence.
            return Detail("unparseable", reason="jobkey_mismatch")
        if body.get("expired") is True or (body.get("jobInfoWrapperModel") or {}).get("expired") is True:
            return Detail("expired", reason="expired_flag")

        info = (body.get("jobInfoWrapperModel") or {}).get("jobInfoModel") or {}
        head = info.get("jobInfoHeaderModel") or {}
        imgs = head.get("companyImagesModel") or {}
        sal = body.get("salaryInfoModel") or {}
        job: dict = {
            "title": clean(first(head.get("jobTitle"), body.get("jobTitle"))),
            "company": clean(head.get("companyName")),
            "company_logo_url": imgs.get("logoUrl") if str(imgs.get("logoUrl") or "").startswith("http") else None,
            "location": clean(first(head.get("formattedLocation"), body.get("jobLocation"))),
            "work_type": normalize_work_type((info.get("jobMetadataHeaderModel") or {}).get("jobType")),
            "description": html_to_text(info.get("sanitizedJobDescription")),
            "posted_at": parse_date(body.get("datePublished")),
        }
        if head.get("remoteLocation") and job["location"] and "remote" not in job["location"].lower():
            job["location"] = f"Remote in {job['location']}"
        if sal.get("salaryText"):
            job["salary_min"], job["salary_max"], job["salary_period"] = parse_salary_text(sal["salaryText"])
        elif sal.get("salaryMin") or sal.get("salaryMax"):
            job["salary_min"], job["salary_max"], job["salary_period"] = finalize_salary(
                sal.get("salaryMin"), sal.get("salaryMax"), _period(sal.get("salaryType")) or "year", fill=False)
        else:
            job["salary_min"] = job["salary_max"] = job["salary_period"] = None
        # The card's absolute pubDate is better than anything on the job view.
        if card and card.get("posted_at"):
            job["posted_at"] = card["posted_at"]
        if not job["posted_at"]:
            job["posted_at"] = parse_relative_date((body.get("hiringInsightsModel") or {}).get("age"))
        job = merge(card, job)
        if not job.get("description") or not job.get("title"):
            return Detail("unparseable", reason="no_description_or_title")
        job["url"] = self.public_url(source_job_id)
        return Detail("live", job)
