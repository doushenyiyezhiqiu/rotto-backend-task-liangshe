"""Find a way to get Indeed UK full job descriptions through the proxy.
Each variant uses a fresh warmed-up session: homepage -> search page -> description request.
About 25 requests in total.

  python -m scripts.indeed_detail_diag
"""
import json
import re
import time
from urllib.parse import quote

from curl_cffi import requests as creq

from crawler import config
from crawler.fetch import classify
from crawler.sources.indeed import Indeed

BASE = "https://uk.indeed.com"
SEARCH = BASE + "/jobs?q=&l=London&sort=date"


def new_session(imp="chrome"):
    s = creq.Session(impersonate=imp)
    p = config.proxy_for_session(config.new_session_id())
    if p:
        s.proxies = {"http": p, "https": p}
    s.headers.update({"Accept-Language": "en-GB,en;q=0.9"})
    return s


def line(label, r):
    blocked, reason = classify(r.status_code, r.text, str(r.url), "indeed")
    t = r.text
    markers = {
        "jobDescriptionText": "jobDescriptionText" in t,
        "sanitizedJobDescription": "sanitizedJobDescription" in t,
        "_initialData": "_initialData" in t,
        "JobPosting": '"JobPosting"' in t,
    }
    found = ",".join(k for k, v in markers.items() if v) or "-"
    print(f"  {label:<36} {r.status_code} blocked={blocked!s:<5} {reason or '':<20} "
          f"bytes={len(r.content):<8} has={found}")
    return r.status_code == 200 and not blocked


def warm_and_search(imp="chrome"):
    s = new_session(imp)
    h = s.get(BASE + "/", timeout=30)
    time.sleep(1.5)
    r = s.get(SEARCH, timeout=30, headers={"Referer": BASE + "/"})
    ok = line(f"search ({imp}, warm={h.status_code})", r)
    jks = [c.source_job_id for c in Indeed().parse_search(r.text, SEARCH).cards] if ok else []
    return s, r, jks


def variant(name, fn, imp="chrome"):
    print(f"\n== {name} ==")
    try:
        s, r, jks = warm_and_search(imp)
        if not jks:
            print("  search failed, skipping")
            return
        time.sleep(1.5)
        fn(s, r, jks)
    except Exception as e:
        print("  ERROR", type(e).__name__, str(e)[:200])


def v1(s, r, jks):  # real navigation: click a result
    nav = {"Referer": SEARCH, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate",
           "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1"}
    for jk in jks[:2]:
        line(f"viewjob jk={jk}", s.get(f"{BASE}/viewjob?jk={jk}", timeout=30, headers=nav))
        time.sleep(2)


def v2(s, r, jks):  # right-hand panel XHR the search page uses
    xhr = {"Referer": SEARCH, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
           "Sec-Fetch-Dest": "empty", "Accept": "application/json, text/plain, */*"}
    jk = jks[0]
    for url in (f"{BASE}/viewjob?viewtype=embedded&jk={jk}&spa=1",
                f"{BASE}/viewjob?jk={jk}&spa=1",
                f"{BASE}/rpc/jobdescs?jks={jk}"):
        line(url.replace(BASE, "")[:36], s.get(url, timeout=30, headers=xhr))
        time.sleep(1.5)


def v3(s, r, jks):  # search page with a job pre-selected (vjk) embeds that job
    jk = jks[0]
    res = s.get(f"{SEARCH}&vjk={jk}", timeout=30, headers={"Referer": SEARCH})
    line(f"search&vjk={jk}", res)


def v4(s, r, jks):  # does the search page itself carry descriptions?
    print("  search page has full descriptions:", "sanitizedJobDescription" in r.text or
          "jobDescriptionText" in r.text)
    snip = re.search(r'"snippet"\s*:\s*"(.{0,120})', r.text)
    print("  snippet sample:", snip.group(1) if snip else None)


def v5_graphql():
    print("\n== GraphQL job details by key (Indeed app API) ==")
    from scripts.indeed_diag import GQL_HEADERS, GQL_URL
    s = new_session("safari18_0_ios")
    q = """query { jobSearch(location: {where: "London", radius: 10, radiusUnit: MILES} limit: 3 sort: DATE)
      { results { job { key title description { html } employer { name dossier { images { squareLogoUrl } } }
      compensation { baseSalary { unitOfWork range { ... on Range { min max } } } }
      attributes { key label } location { formatted { long } } datePublished } } } }"""
    try:
        r = s.post(GQL_URL, headers=GQL_HEADERS, data=json.dumps({"query": q}), timeout=30)
        print(f"  status={r.status_code} bytes={len(r.content)}")
        data = r.json()
        if "errors" in data:
            print("  errors:", json.dumps(data["errors"])[:400])
        for res in (data.get("data") or {}).get("jobSearch", {}).get("results", [])[:2]:
            j = res["job"]
            desc = (j.get("description") or {}).get("html") or ""
            j["description"] = f"<{len(desc)} chars>"
            print("  ", json.dumps(j, default=str)[:600])
    except Exception as e:
        print("  ERROR", type(e).__name__, str(e)[:200])


def main():
    variant("V1 click through to /viewjob (chrome)", v1)
    variant("V1b click through to /viewjob (safari)", v1, imp="safari18_0")
    variant("V2 XHR endpoints the search page uses", v2)
    variant("V3 search page with vjk (job preselected)", v3)
    variant("V4 descriptions inside search page", v4)
    v5_graphql()


if __name__ == "__main__":
    main()
