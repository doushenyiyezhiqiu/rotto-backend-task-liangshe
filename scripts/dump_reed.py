"""Print the raw job objects embedded in a saved Reed page (debug/probe-reed-*.html), minus long
text, so parser field names can be checked against real data.

  python -m scripts.dump_reed debug/probe-reed-search.html [N]
"""
import json
import sys

from crawler.sources.reed import next_data
from crawler.textutil import walk


def short(v):
    if isinstance(v, str) and len(v) > 160:
        return v[:160] + "..."
    if isinstance(v, dict):
        return {k: short(x) for k, x in v.items()}
    if isinstance(v, list):
        return [short(x) for x in v[:5]]
    return v


def main():
    path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    html = open(path, encoding="utf-8").read()
    nd = next_data(html)
    if not nd:
        print("no __NEXT_DATA__ found")
        return 1
    shown = 0
    for d in walk(nd):
        inner = d.get("jobDetail") if isinstance(d.get("jobDetail"), dict) else None
        if (inner or d).get("jobId") and (inner or d).get("jobTitle"):
            print(json.dumps(short(d), indent=1, default=str))
            print("-" * 60)
            shown += 1
            if shown >= n:
                break
    # Logo-ish and salary-ish keys anywhere in the page data, to locate fields we miss.
    keys = {}
    for d in walk(nd):
        for k, v in d.items():
            kl = k.lower()
            if any(w in kl for w in ("logo", "salary", "image")) and k not in keys:
                keys[k] = short(v)
    print("logo/salary/image keys seen:", json.dumps(keys, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
