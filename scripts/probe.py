"""Fetch one live URL through the proxy, save the raw HTML to debug/, and print what the
parser extracts. Use it whenever a board changes its markup.

  python -m scripts.probe linkedin "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?location=United%20Kingdom&start=0" search
  python -m scripts.probe reed "https://www.reed.co.uk/jobs/data-engineer/51234567" detail
"""
import json
import sys
from pathlib import Path

from crawler import config
from crawler.fetch import Fetcher, RequestLog
from crawler.sources import get_source


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    name, url = sys.argv[1], sys.argv[2]
    kind = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else ("search" if "search" in url or "jobs?" in url else "detail")
    src = get_source(name)
    log = RequestLog(None, 0)
    _add = log.add

    def show_attempt(**kw):  # print every attempt, warm-ups included
        print(f"  [{kw['kind']:<7} try {kw['attempt']}] status={kw['status']} blocked={kw['blocked']} "
              f"reason={kw['reason']} bytes={kw['nbytes']}")
        _add(**kw)

    log.add = show_attempt
    f = Fetcher(name, log, referer=src.referer, extra_headers=src.extra_headers,
                warmup_url=src.warmup_url, impersonate=src.impersonate)
    res = f.get(url, kind)
    print(f"status={res.status} blocked={res.blocked} reason={res.reason or res.error} bytes={len(res.text)} final={res.final_url}")
    out = Path(config.DEBUG_DIR) / f"probe-{name}-{kind}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(res.text, encoding="utf-8")
    print(f"saved {out}")
    if not res.ok:
        return 1
    if kind == "search":
        sp = src.parse_search(res.text, url)
        print(f"{len(sp.cards)} cards, has_more={sp.has_more}")
        for c in sp.cards[:5]:
            print(json.dumps({"id": c.source_job_id, "url": c.url, **c.fields}, default=str, indent=1))
    else:
        sjid = sys.argv[4] if len(sys.argv) > 4 else url.rstrip("/").split("/")[-1].split("jk=")[-1].split("?")[0]
        d = src.parse_detail(res.text, res.final_url or url, sjid, None)
        job = dict(d.job or {})
        if job.get("description"):
            job["description"] = job["description"][:400] + "..."
        print(d.status, d.reason)
        print(json.dumps(job, default=str, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
