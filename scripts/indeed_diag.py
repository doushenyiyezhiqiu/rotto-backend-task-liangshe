"""Try several ways of reaching Indeed UK through the proxy and report which ones get through.
Uses ~20 small requests. Results decide how the Indeed source is built.

  python -m scripts.indeed_diag
"""
import json
import time

import curl_cffi
from curl_cffi import requests as creq

from crawler import config
from crawler.fetch import classify

SEARCH = "https://uk.indeed.com/jobs?q=&l=London&sort=date"
TARGETS = ["chrome", "chrome131", "chrome133a", "chrome124", "safari18_0", "safari17_0", "firefox133",
           "chrome131_android", "safari18_0_ios"]

GQL_URL = "https://apis.indeed.com/graphql"
GQL_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "indeed-api-key": "161092c2017b5bbab13edb12461a62d5a833871e7cad6d9d475304573de67ac8",
    "indeed-locale": "en-GB",
    "indeed-co": "GB",
    "accept-language": "en-GB,en;q=0.9",
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Mobile/15E148 Indeed App 193.1",
    "indeed-app-info": "appv=193.1; appid=com.indeed.jobsearch; osv=16.6.1; os=ios; dtype=phone",
}
GQL_QUERY = """query GetJobData { jobSearch(location: {where: "London", radius: 10, radiusUnit: MILES}
  limit: 5 sort: DATE) { pageInfo { nextCursor } results { job { key title datePublished
  location { formatted { long } } employer { name } description { html } } } } }"""


def session(imp):
    s = creq.Session(impersonate=imp)
    p = config.proxy_for_session(config.new_session_id())
    if p:
        s.proxies = {"http": p, "https": p}
    s.headers.update({"Accept-Language": "en-GB,en;q=0.9"})
    return s


def show(label, r=None, err=None):
    if err:
        print(f"{label:<42} ERROR {err[:90]}")
        return False
    blocked, reason = classify(r.status_code, r.text, str(r.url), "indeed")
    has_cards = "mosaic-provider-jobcards" in r.text
    ok = r.status_code == 200 and not blocked
    print(f"{label:<42} {r.status_code} blocked={blocked!s:<5} {reason or '':<22} "
          f"bytes={len(r.content):<7} cards_json={has_cards}")
    return ok


def try_get(label, imp, url, warm=False):
    try:
        s = session(imp)
        if warm:
            s.get("https://uk.indeed.com/", timeout=30)
            time.sleep(2)
        return show(label, s.get(url, timeout=30, headers={"Referer": "https://uk.indeed.com/"}))
    except Exception as e:
        return show(label, err=f"{type(e).__name__}: {e}")


def main():
    print("curl_cffi", curl_cffi.__version__, "| proxy:", "yes" if config.base_proxy_url() else "NO")
    print("\n== A. desktop search page, different browser fingerprints ==")
    for imp in TARGETS:
        try_get(f"A {imp}", imp, SEARCH)
    print("\n== B. warm up on homepage first (cookies), then search ==")
    for imp in ("chrome", "safari18_0"):
        try_get(f"B warm {imp}", imp, SEARCH, warm=True)
    print("\n== C. mobile site ==")
    for imp in ("chrome131_android", "safari18_0_ios"):
        try_get(f"C mobile {imp}", imp, "https://uk.indeed.com/m/jobs?q=&l=London")
    print("\n== D. job page directly ==")
    try_get("D viewjob chrome", "chrome", "https://uk.indeed.com/viewjob?jk=0000000000000000")
    print("\n== E. Indeed app GraphQL API ==")
    try:
        s = session("safari18_0_ios")
        r = s.post(GQL_URL, headers=GQL_HEADERS, data=json.dumps({"query": GQL_QUERY}), timeout=30)
        print(f"E graphql {r.status_code} bytes={len(r.content)}")
        try:
            data = r.json()
            res = data["data"]["jobSearch"]["results"]
            print(f"  {len(res)} jobs; first:", json.dumps(
                {k: v for k, v in res[0]["job"].items() if k != "description"}, default=str)[:400])
            print("  has description:", bool(res[0]["job"].get("description", {}).get("html")))
        except Exception:
            print("  body:", r.text[:400])
    except Exception as e:
        print("E graphql ERROR", type(e).__name__, str(e)[:200])


if __name__ == "__main__":
    main()
