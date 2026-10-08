"""LinkedIn Jobs via the logged-out "guest" endpoints that linkedin.com/jobs itself uses:
  search: /jobs-guest/jobs/api/seeMoreJobPostings/search  (HTML fragment of job cards)
  detail: /jobs-guest/jobs/api/jobPosting/<id>             (HTML fragment of the job page)
Both are public, need no login, and are far smaller than the full /jobs/view pages."""
from __future__ import annotations

import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..textutil import (clean, html_to_text, normalize_work_type, parse_date,
                        parse_relative_date, parse_salary_text)
from .base import Card, Detail, SearchPage, Shard, Source, merge, text_of
from .locations import BIG_LOCATIONS, KEYWORDS, UK_LOCATIONS

BASE = "https://www.linkedin.com"
UK_GEO_ID = "101165590"
JOB_TYPES = ["F", "P", "C", "T", "I", "V", "O"]   # full, part, contract, temp, intern, volunteer, other
URN = re.compile(r"jobPosting:(\d+)")
VIEW_ID = re.compile(r"/jobs/view/(?:[^/?]*-)?(\d{6,})")


def _logo(img) -> str | None:
    if img is None:
        return None
    for attr in ("data-delayed-url", "data-ghost-url", "src"):
        v = img.get(attr)
        if v and v.startswith("http") and "ghost" not in v and "data:image" not in v:
            return v
    return None


class LinkedIn(Source):
    name = "linkedin"
    referer = BASE + "/jobs/search"
    min_interval = 1.5
    extra_headers = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

    def shards(self):
        out = []
        for jt in JOB_TYPES:
            out.append(Shard(f"uk:{jt}", {"location": "United Kingdom", "geoId": UK_GEO_ID, "f_JT": jt}))
        for loc in UK_LOCATIONS:
            out.append(Shard(f"loc:{loc}", {"location": f"{loc}, United Kingdom"}))
        for loc in BIG_LOCATIONS:
            for kw in KEYWORDS:
                if kw:
                    out.append(Shard(f"kw:{kw}@{loc}", {"keywords": kw, "location": f"{loc}, United Kingdom"}))
        return out

    def search_url(self, shard: Shard, page: int) -> str:
        q = {"keywords": shard.params.get("keywords", ""), "location": shard.params["location"],
             "sortBy": "DD", "start": (page - 1) * 10}
        if shard.params.get("geoId"):
            q["geoId"] = shard.params["geoId"]
        if shard.params.get("f_JT"):
            q["f_JT"] = shard.params["f_JT"]
        return f"{BASE}/jobs-guest/jobs/api/seeMoreJobPostings/search?{urlencode(q)}"

    def parse_search(self, html: str, url: str) -> SearchPage:
        soup = BeautifulSoup(html, "lxml")
        cards: dict[str, Card] = {}
        for el in soup.select("[data-entity-urn*='jobPosting'], div.base-card, li"):
            urn = el.get("data-entity-urn") or ""
            m = URN.search(urn)
            link = el.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
            if not m and link is not None:
                m = VIEW_ID.search(link.get("href", ""))
            if not m:
                continue
            jid = m.group(1)
            if jid in cards:
                continue
            sal_text = text_of(el.select_one(".job-search-card__salary-info"))
            lo, hi, period = parse_salary_text(sal_text)
            t = el.select_one("time")
            f = {
                "title": text_of(el.select_one(".base-search-card__title, h3")),
                "company": text_of(el.select_one(".base-search-card__subtitle, h4")),
                "location": text_of(el.select_one(".job-search-card__location")),
                "company_logo_url": _logo(el.select_one("img.artdeco-entity-image, img")),
                "salary_text": sal_text, "salary_min": lo, "salary_max": hi, "salary_period": period,
                "posted_at": parse_date(t.get("datetime")) if t is not None and t.get("datetime") else None,
            }
            cards[jid] = Card(jid, self.public_url(jid), f)
        return SearchPage(list(cards.values()), has_more=len(cards) > 0)

    def detail_url(self, source_job_id: str, stored_url: str | None) -> str:
        return f"{BASE}/jobs-guest/jobs/api/jobPosting/{source_job_id}"

    def public_url(self, source_job_id: str, stored_url: str | None = None) -> str:
        return f"{BASE}/jobs/view/{source_job_id}"

    def parse_detail(self, html: str, url: str, source_job_id: str, card: dict | None) -> Detail:
        soup = BeautifulSoup(html, "lxml")
        low = html.lower()
        if "no longer accepting applications" in low or soup.select_one(".closed-job"):
            return Detail("expired", reason="closed_job")
        job: dict = {}
        job["title"] = text_of(soup.select_one(".top-card-layout__title, .topcard__title, h2"))
        job["company"] = text_of(soup.select_one(
            "a.topcard__org-name-link, .topcard__org-name-link, span.topcard__flavor"))
        job["location"] = text_of(soup.select_one(".topcard__flavor--bullet"))
        job["company_logo_url"] = _logo(soup.select_one(
            ".top-card-layout img.artdeco-entity-image, img.artdeco-entity-image"))
        desc = soup.select_one(".show-more-less-html__markup, .description__text")
        job["description"] = html_to_text(desc.decode_contents()) if desc is not None else None
        sal_text = text_of(soup.select_one(".salary.compensation__salary, .compensation__salary"))
        if sal_text:
            job["salary_min"], job["salary_max"], job["salary_period"] = parse_salary_text(sal_text)
        job["posted_at"] = parse_relative_date(text_of(soup.select_one(".posted-time-ago__text")))
        for item in soup.select(".description__job-criteria-item"):
            label = (text_of(item.select_one(".description__job-criteria-subheader, h3")) or "").lower()
            if "employment type" in label:
                job["work_type"] = normalize_work_type(
                    text_of(item.select_one(".description__job-criteria-text, span")))
        # Card date (absolute, from <time datetime>) is better than "2 weeks ago".
        if card and card.get("posted_at"):
            job["posted_at"] = card["posted_at"]
        job = merge(card, job)
        if not job.get("description") or not job.get("title"):
            return Detail("unparseable", reason="no_description_or_title")
        job["url"] = self.public_url(source_job_id)
        return Detail("live", job)
