"""Offline parser/unit tests on small synthetic pages shaped like each board's markup.
Run with `make unit`. Real pages drift; use `make probe` against live URLs to re-check."""
import json
import unittest
from datetime import datetime, timezone

from crawler.dedupe import containment, norm_company, norm_title, tokens
from crawler.sources.indeed import Indeed
from crawler.sources.linkedin import LinkedIn
from crawler.sources.reed import Reed
from crawler.textutil import html_to_text, normalize_work_type, parse_relative_date, parse_salary_text
from crawler.validate import is_uk_listing

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
DESC = "<p>We are hiring a <b>Data Engineer</b>.</p><ul><li>Python</li><li>Spark</li></ul><p>Apply today for this great role in our team.</p>"


class TextUtil(unittest.TestCase):
    def test_salary(self):
        self.assertEqual(parse_salary_text("£30,000 - £35,000 per annum"), (30000, 35000, "year"))
        self.assertEqual(parse_salary_text("£12.50 per hour"), (12.5, 12.5, "hour"))
        self.assertEqual(parse_salary_text("£450 - £550 per day"), (450, 550, "day"))
        self.assertEqual(parse_salary_text("£40k-£50k"), (40000, 50000, "year"))
        self.assertEqual(parse_salary_text("£2,500 per month"), (30000, 30000, "year"))
        self.assertEqual(parse_salary_text("Competitive"), (None, None, None))
        self.assertEqual(parse_salary_text("From £27,293 a year"), (27293, None, "year"))
        self.assertEqual(parse_salary_text("Up to £40,000 a year"), (None, 40000, "year"))

    def test_work_type(self):
        self.assertEqual(normalize_work_type("Permanent, full-time"), "full time")
        self.assertEqual(normalize_work_type("Contract, full-time"), "contract")
        self.assertEqual(normalize_work_type("PART_TIME"), "part time")
        self.assertIsNone(normalize_work_type("Permanent"))

    def test_relative_date(self):
        self.assertEqual(parse_relative_date("Posted 3 days ago", NOW).date().isoformat(), "2026-10-04")
        self.assertEqual(parse_relative_date("2 weeks ago", NOW).date().isoformat(), "2026-09-23")
        self.assertEqual(parse_relative_date("Just posted", NOW).date(), NOW.date())

    def test_html_to_text(self):
        t = html_to_text(DESC)
        self.assertIn("Data Engineer", t)
        self.assertIn("• Python", t)
        self.assertNotIn("<", t)


class Validity(unittest.TestCase):
    def test_uk(self):
        self.assertTrue(is_uk_listing("linkedin", "London, England, United Kingdom", "")[0])
        self.assertFalse(is_uk_listing("linkedin", "Dublin, County Dublin, Ireland", "")[0])
        self.assertTrue(is_uk_listing("reed", "Hemel Hempstead", "")[0])
        self.assertFalse(is_uk_listing("indeed", "Remote", "work from anywhere")[0])
        self.assertTrue(is_uk_listing("indeed", "Remote", "Open to candidates based in the UK")[0])
        self.assertFalse(is_uk_listing("linkedin", "Sydney, New South Wales, Australia", "")[0])


class LinkedInParse(unittest.TestCase):
    SEARCH = """<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4012345678">
      <a class="base-card__full-link" href="https://uk.linkedin.com/jobs/view/data-engineer-at-acme-4012345678?refId=x"></a>
      <img class="artdeco-entity-image" data-delayed-url="https://media.licdn.com/dms/image/logo.png">
      <h3 class="base-search-card__title"> Data Engineer </h3>
      <h4 class="base-search-card__subtitle"><a>Acme</a></h4>
      <span class="job-search-card__location">London, England, United Kingdom</span>
      <span class="job-search-card__salary-info">£60,000.00/yr - £70,000.00/yr</span>
      <time class="job-search-card__listdate" datetime="2026-10-05">2 days ago</time></div></li>"""
    DETAIL = f"""<section class="top-card-layout"><img class="artdeco-entity-image" data-delayed-url="https://media.licdn.com/dms/image/logo.png">
      <h2 class="top-card-layout__title">Data Engineer</h2>
      <span class="topcard__flavor"><a class="topcard__org-name-link">Acme</a></span>
      <span class="topcard__flavor topcard__flavor--bullet">London, England, United Kingdom</span>
      <span class="posted-time-ago__text">2 days ago</span></section>
      <div class="show-more-less-html__markup">{DESC}</div>
      <ul class="description__job-criteria-list"><li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text">Full-time</span></li></ul>"""

    def test_search_and_detail(self):
        src = LinkedIn()
        sp = src.parse_search(self.SEARCH, "")
        self.assertEqual(len(sp.cards), 1)
        c = sp.cards[0]
        self.assertEqual(c.source_job_id, "4012345678")
        self.assertEqual(c.url, "https://www.linkedin.com/jobs/view/4012345678")
        self.assertEqual(c.fields["salary_min"], 60000)
        d = src.parse_detail(self.DETAIL, "", "4012345678", c.fields)
        self.assertEqual(d.status, "live")
        self.assertEqual(d.job["company"], "Acme")
        self.assertEqual(d.job["work_type"], "full time")
        self.assertEqual(d.job["posted_at"].date().isoformat(), "2026-10-05")
        self.assertIn("Spark", d.job["description"])

    def test_closed(self):
        d = LinkedIn().parse_detail("<figcaption>No longer accepting applications</figcaption>", "", "1", None)
        self.assertEqual(d.status, "expired")


class IndeedParse(unittest.TestCase):
    """Shapes mirror live uk.indeed.com pages (Oct 2026)."""
    CARD = {"jobkey": "abc123def456", "displayTitle": "Data Engineer", "company": "Acme",
            "formattedLocation": "London WC2E 9DA", "pubDate": 1786510800000,
            "extractedSalary": {"min": -1, "max": 60000, "type": "YEARLY"},
            "salarySnippet": {"currency": "GBP", "text": "From £60,000 a year"},
            "jobTypes": ["Full-time", "Permanent"]}

    def search_html(self, embedded_key="abc123def456"):
        cards = {"metaData": {"mosaicProviderJobCardsModel": {"results": [self.CARD]}}}
        init = {"autoOpenTwoPaneJobKey": embedded_key, "autoOpenTwoPaneViewjobResponse": {"body": {
            "jobKey": embedded_key, "jobTitle": "Data Engineer", "datePublished": None,
            "salaryInfoModel": {"salaryText": "From £60,000 a year", "salaryMin": 60000,
                                "salaryMax": 60000, "salaryType": "YEARLY"},
            "hiringInsightsModel": {"age": "30+ days ago"},
            "jobInfoWrapperModel": {"jobInfoModel": {
                "jobInfoHeaderModel": {"jobTitle": "Data Engineer", "companyName": "Acme",
                                       "formattedLocation": "London WC2E 9DA",
                                       "companyImagesModel": {"logoUrl": "https://d2q79iu7y748jz.cloudfront.net/s/_squarelogo/x.png"}},
                "jobMetadataHeaderModel": {"jobType": "Permanent, Full-time"},
                "sanitizedJobDescription": DESC}}}}}
        return ('<script>window.mosaic.providerData["mosaic-provider-jobcards"]=' + json.dumps(cards)
                + ';</script><script>window._initialData=' + json.dumps(init)
                + ';</script><a data-testid="pagination-page-next">')

    def test_search_card(self):
        sp = Indeed().parse_search(self.search_html(), "")
        self.assertTrue(sp.has_more)
        c = sp.cards[0]
        self.assertEqual(c.url, "https://uk.indeed.com/viewjob?jk=abc123def456")
        self.assertEqual((c.fields["salary_min"], c.fields["salary_max"]), (60000, None))
        self.assertEqual(c.fields["work_type"], "full time")

    def test_detail_from_embedded_view(self):
        src = Indeed()
        self.assertIn("vjk=abc123def456", src.detail_url("abc123def456", None))
        c = src.parse_search(self.search_html(), "").cards[0]
        d = src.parse_detail(self.search_html(), "", "abc123def456", c.fields)
        self.assertEqual(d.status, "live")
        self.assertEqual(d.job["salary_min"], 60000)
        self.assertIsNone(d.job["salary_max"])
        self.assertEqual(d.job["company_logo_url"][:8], "https://")
        self.assertEqual(d.job["posted_at"].year, 2026)
        self.assertIn("Spark", d.job["description"])

    def test_wrong_embedded_job_is_not_used(self):
        d = Indeed().parse_detail(self.search_html(embedded_key="zzzz"), "", "abc123def456", None)
        self.assertEqual(d.status, "unparseable")


class ReedParse(unittest.TestCase):
    """Shapes mirror live reed.co.uk pages (Oct 2026)."""
    ITEM = {"jobDetail": {"jobId": 53197606, "jobTitle": "Deputy Manager", "displayDate": "2026-09-23T00:11:07",
                          "dateCreated": "2024-07-30T11:42:11", "displayLocationName": "Enfield Town",
                          "countyLocation": "Middlesex", "isFullTime": True, "isPartTime": False,
                          "ouName": "Aldi", "salaryDescription": 0, "salaryFrom": 16.13, "salaryTo": 16.45,
                          "salaryType": 1},
            "profileName": "Aldi", "url": "/jobs/deputy-manager/53197606", "logoImage": "Logo_30089.png"}
    HIDDEN = {"jobDetail": {"jobId": 57439358, "jobTitle": "C++ Developer", "displayDate": "2026-10-08T20:12:46",
                            "displayLocationName": "London", "countyLocation": "London", "isFullTime": True,
                            "ouName": "eFinancialCareers", "salaryDescription": 64, "salaryFrom": 20000,
                            "salaryTo": 100000, "salaryType": 5}, "url": "/jobs/c-developer/57439358"}

    def test_search(self):
        nd = {"props": {"pageProps": {"searchResults": {"count": 99775, "jobs": [self.HIDDEN],
                                                        "promotedJobs": [self.ITEM]}}}}
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        sp = Reed().parse_search(html, "")
        self.assertEqual(sp.total, 99775)
        cards = {c.source_job_id: c for c in sp.cards}
        aldi = cards["53197606"].fields
        self.assertEqual(aldi["location"], "Enfield Town, Middlesex")
        self.assertEqual((aldi["salary_min"], aldi["salary_period"]), (16.13, "hour"))
        self.assertEqual(aldi["posted_at"].isoformat(), "2026-09-23T00:11:07+01:00")
        self.assertTrue(aldi["company_logo_url"].endswith("/logos/thumbs/Logo_30089.png"))
        self.assertIsNone(cards["57439358"].fields["salary_min"])  # "Competitive salary"
        self.assertEqual(cards["57439358"].fields["location"], "London")

    def test_detail(self):
        jd = {"id": 53197606, "title": "Deputy Manager", "description": DESC, "isLive": True,
              "createdDate": "2024-07-30T11:42:11", "displayDate": "2026-09-23T00:11:07",
              "jobOwner": {"profileName": "Aldi", "ouName": "Aldi", "profileLogoImage": "Logo_30089.png"},
              "jobContractType": {"id": 1, "name": "Permanent"},
              "jobEmploymentHours": {"isFullTime": True, "isPartTime": False},
              "jobLocation": {"locationName": "Enfield Town", "countyName": "Middlesex",
                              "regionName": "South East England", "isRemoteJob": False},
              "jobSalary": {"displaySalary": "£ 16.13 - £ 16.45 per hour", "from": 16.13, "to": 16.45}}
        nd = {"props": {"pageProps": {"consolidatedJobDetails": {"jobDetails": jd},
                                      "canonicalUrl": "https://www.reed.co.uk/jobs/deputy-manager/53197606"}}}
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        src = Reed()
        d = src.parse_detail(html, "", "53197606", None)
        self.assertEqual(d.status, "live")
        self.assertEqual(d.job["location"], "Enfield Town, Middlesex")
        self.assertEqual(d.job["posted_at"].date().isoformat(), "2026-09-23")
        self.assertEqual((d.job["salary_min"], d.job["salary_max"], d.job["salary_period"]), (16.13, 16.45, "hour"))
        self.assertEqual(d.job["work_type"], "full time")
        jd["jobContractType"] = {"id": 2, "name": "Contract"}
        self.assertEqual(src.parse_detail(html.replace('"Permanent"', '"Contract"'), "", "53197606", None).job["work_type"], "contract")
        expired = html.replace('"isLive": true', '"isLive": false')
        self.assertEqual(src.parse_detail(expired, "", "53197606", None).status, "expired")


class Dedupe(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(norm_company("Acme Ltd"), norm_company("ACME Limited"))
        self.assertEqual(norm_title("Data Engineer - London (Hybrid)"), "data engineer")
        a = tokens("Build batch and streaming pipelines on AWS with Python Spark Airflow Snowflake")
        b = tokens("You will build streaming and batch pipelines using Python, Spark, Airflow on AWS and Snowflake")
        self.assertGreater(containment(a, b), 0.8)


if __name__ == "__main__":
    unittest.main()
